import argparse
import json
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

from .adapters import ADAPTERS
from .config import initialize, load_config
from .locations import catalogue
from .comparison import compare
from .browser import load_browser_evidence
from .observations import load_review


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local retailer search with source evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create private settings without overwriting existing files")
    init.add_argument("--market-id", required=True)
    init.add_argument("--market-name", default="")
    init.add_argument("--market-address", default="")
    init.add_argument("--latitude", type=float)
    init.add_argument("--longitude", type=float)
    init.add_argument("--json", action="store_true")
    search = sub.add_parser("search", help="Search the first public result page")
    search.add_argument("query")
    search.add_argument("--retailer", choices=sorted(ADAPTERS), default="mediamarkt")
    search.add_argument("--limit", type=int, default=12)
    search.add_argument("--json", action="store_true", help="Emit schema-versioned JSON")
    shops = sub.add_parser("shops", help="Show online whitelist and local candidate catalogue")
    shops.add_argument("--json", action="store_true")
    comparison = sub.add_parser("compare", help="Compare a concrete MediaMarkt URL or unambiguous EAN")
    comparison.add_argument("reference")
    comparison.add_argument("--offer-url", action="append", default=[], help="Canonical product URL of a supported approved retailer; repeatable")
    comparison.add_argument("--browser-evidence", action="append", default=[], metavar="FILE",
                            help="Use fresh browser JSON-LD captures from FILE or - for stdin; repeatable")
    comparison.add_argument("--json", action="store_true")
    comparison.add_argument("--review-evidence", metavar="FILE", help="Fresh public market and local advertisement evidence")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if (args.latitude is None) != (args.longitude is None):
                raise ValueError("Supply both latitude and longitude")
            market = None
            if args.market_name or args.market_address or args.latitude is not None:
                market = {"name": args.market_name, "address": args.market_address,
                          "coordinates": [args.latitude, args.longitude] if args.latitude is not None else None}
            initialize(args.market_id, market)
            result = {"schema_version": 1, "status": "ok", "market_id": args.market_id}
            print(json.dumps(result) if args.json else "Lokale Konfiguration erstellt; bestehende Dateien werden nicht überschrieben.")
        elif args.command == "search":
            query = args.query.strip()
            if not query or len(query) > 200 or any(ord(c) < 32 for c in query):
                raise ValueError("Search query must contain 1 to 200 printable characters")
            if not 1 <= args.limit <= 100:
                raise ValueError("Limit must be between 1 and 100")
            settings = load_config()
            source, products = ADAPTERS[args.retailer]().search(query)
            matched = [p for p in products if p["query_token_match"]]
            suggestions = [p for p in products if not p["query_token_match"]]
            result = {"schema_version": 1, "status": "ok", "query": query,
                      "source": source, "observed_at": datetime.now(timezone.utc).isoformat(),
                      "market_id": settings["market_id"], "scope": "first_search_page",
                      "parsed_count": len(products), "matched_count": len(matched),
                      "match_method": "all_literal_query_tokens",
                      "returned_count": min(len(matched), args.limit),
                      "products": matched[:args.limit], "suggestions": suggestions[:args.limit]}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"MediaMarkt | Markt {settings['market_id'] or 'nicht eingerichtet'} | erste Ergebnisseite | {source}")
                if not matched:
                    print("Keine Treffer mit allen Suchbegriffen auf der ersten Ergebnisseite.")
                if suggestions:
                    print(f"{len(suggestions)} weitere Empfehlungen separat in --json; kein passender Suchtreffer bestätigt.")
                for p in result["products"]:
                    print(f"{p['price'] or '?'} {p['currency'] or '?'} | {p['name']}")
                    print(f"  Verkäufer: {p['seller'] or '?'} ({p['seller_kind']}); online: {p['online_availability']}; Versand: {p['shipping'] or '?'}")
                    print(f"  EAN: {p['ean'] or '?'} | {p['url']}")
                print("Bestand im gewählten Markt und Preisversprechen noch nicht geprüft.")
        elif args.command == "compare":
            settings = load_config()
            captures = load_browser_evidence(args.browser_evidence)
            review = load_review(args.review_evidence) if args.review_evidence else None
            result = compare(args.reference, settings["market_id"], args.offer_url, captures, review)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result["product"]["name"] + " | EAN " + result["product"]["ean"])
                for offer in result["offers"]:
                    print(f"{offer['price'] or '?'} {offer['currency'] or '?'} | {offer['retailer']} | {offer['seller'] or '?'} | {offer['assessment']['status']}")
                    print("  " + ", ".join(offer['assessment']['excluded_reasons'] + offer['assessment']['review_reasons']))
                    print("  " + offer["url"])
                print("Händlerprüfung: " + ", ".join(c["retailer"] + "=" + c["status"] for c in result["retailer_checks"]))
                for request in result["fallback_requests"]:
                    print("Browser-Fallback erforderlich: " + request["search_url"])
                first = result["review_first"]
                print("Zuerst manuell prüfen: " + (first["url"] if first else "kein prüfbarer Kandidat"))
                print("Teilabdeckung; Marktpreis, Bestand, Mitgliedschaft und Ausschlüsse offen. Keine Zusage des Preisversprechens.")
                print(result["policy_source"])
                if result["review_evidence"]:
                    print("Manuelle Markt-/Prospektbelege vorhanden; Details und offene Prüfungen in --json.")
        else:
            result = catalogue()
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                market = result["market"]
                print("Bezugsmarkt: " + (market["name"] + ", " + market["address"] if market else "nicht eingerichtet"))
                print("Online: " + ", ".join(result["online_domains"]))
                print("Direktangebote, sofort verfügbar, Lieferung innerhalb von 3 Tagen; weitere Ausschlüsse beachten.")
                for shop in result["local_candidates"]:
                    flag = "Grenzfall, manuell prüfen" if shop["radius_assessment"] == "borderline" else (
                        "im Radius" if shop["within_30km_airline"] else "außerhalb")
                    print(f"{shop['distance_km']:.1f} km | {flag} | {shop['name']} | {shop['address']}")
                    if shop["coordinate_precision"] != "address":
                        print("  Entfernung nur näherungsweise aus Straßenkoordinaten.")
                    if "Apple" in shop["focus"]:
                        print("  Schwerpunkt Apple; Apple-Produkte sind ausgeschlossen.")
                if result["local_catalogue_status"] != "available":
                    print("Für diesen Markt ist kein lokaler Händlerkatalog verfügbar. Online-Liste bleibt nutzbar.")
                print("Luftlinie anhand Geodaten; keine Zusage der Anerkennung. Katalog ist nicht vollständig.")
                print("Lokale Preise/Bestände noch nicht abgefragt. Regelstand: " + result["checked_on"])
                print(result["policy_source"])
        return 0
    except (ValueError, TypeError, KeyError, AttributeError, URLError, OSError) as exc:
        # Raw OS/network errors may contain proxy details or local paths.
        message = "HTTP " + str(exc.code) if isinstance(exc, HTTPError) else "Network or filesystem request failed" if isinstance(exc, (URLError, OSError)) else (
            "Retailer response schema is invalid" if isinstance(exc, (TypeError, KeyError, AttributeError)) else str(exc))
        status = "source_gone" if isinstance(exc, HTTPError) and exc.code in (404, 410) else "failed"
        error = {"schema_version": 1, "status": status, "error": message}
        stream = sys.stdout if args.json else sys.stderr
        print(json.dumps(error, ensure_ascii=False) if args.json else "Fehler: " + message, file=stream)
        return 1
