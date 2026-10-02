"""Evidence-led comparison with explicit incomplete coverage and review reasons."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from .adapters.competitors import COMPETITORS
from .adapters.http import SourceError
from .adapters.mediamarkt import MediaMarkt
from .offers import HOSTS, gtin


def resolve_product(reference):
    adapter = MediaMarkt()
    if reference.startswith("https://"):
        return adapter.product(reference)
    code = gtin(reference)
    _, candidates = adapter.search(reference)
    exact = [p for p in candidates if p.get("ean") and gtin(p["ean"]) == code]
    if len(exact) != 1:
        raise SourceError("EAN selection is missing or ambiguous; select a concrete MediaMarkt URL")
    product = adapter.product(exact[0]["url"])
    if product["gtin"] != code:
        raise SourceError("Search and product page disagree on GTIN")
    return product


def assess(offer, product):
    excluded, review = [], []
    if offer["gtin"] != product["gtin"]:
        excluded.append("different_gtin")
    if offer["seller_kind"] == "marketplace":
        excluded.append("marketplace_seller")
    elif offer["seller_kind"] != "direct":
        review.append("seller_unknown")
    if offer["condition"] in ("used", "refurbished"):
        excluded.append("condition_excluded")
    elif offer["condition"] != "new":
        review.append("condition_unknown")
    if offer["availability"] == "unavailable":
        excluded.append("not_immediately_available")
    elif offer["availability"] != "available":
        review.append("availability_unknown")
    if offer["price"] is None or offer["currency"] != "EUR":
        review.append("comparable_eur_price_missing")
    if any(isinstance(brand, str) and brand.strip().casefold() == "apple"
           for brand in (offer["brand"], product.get("brand"))):
        excluded.append("apple_excluded")
    delivery = offer["delivery"]
    advertised = offer.get("advertised_delivery")
    if (delivery and delivery["min_days"] > 3) or (advertised and advertised["calendar_days"] > 3):
        excluded.append("delivery_exceeds_three_days")
    else:
        review.append("delivery_within_three_calendar_days_unconfirmed")
    # Online prices cannot establish the selected physical market's selling price.
    review.extend(["market_stock_and_price_unconfirmed", "membership_unconfirmed",
                   "bundle_campaign_and_other_exclusions_unconfirmed"])
    return {"status": "excluded" if excluded else "needs_review",
            "excluded_reasons": excluded, "review_reasons": review,
            "identity_match": "exact_gtin" if offer["gtin"] == product["gtin"] else "mismatch"}


def source_failure(exc):
    if isinstance(exc, HTTPError):
        return {"status": "blocked" if exc.code in (403, 429) else "failed",
                "error": "HTTP " + str(exc.code)}
    if isinstance(exc, (URLError, OSError)):
        return {"status": "failed", "error": "Network request failed"}
    return {"status": "failed", "error": "Retailer response could not be verified"}


def compare(reference, market_id, offer_urls=(), browser_captures=(), review_evidence=None):
    supplied = {name: [] for name in COMPETITORS}
    for url in offer_urls:
        retailer = next((r for r in COMPETITORS if urlsplit(url).hostname == HOSTS[r]), None)
        if retailer is None:
            raise SourceError("Competitor URL must belong to an implemented retailer")
        from .adapters.http import validate_url
        validate_url(url, HOSTS[retailer])
        supplied[retailer].append(url)
    if sum(map(len, supplied.values())) > 12:
        raise SourceError("At most 12 competitor URLs are supported")
    product = resolve_product(reference)
    from .observations import validate_review
    review = validate_review(review_evidence, product, market_id) if review_evidence is not None else None
    policy = json.loads((Path(__file__).parent / "data/retailers.json").read_text())
    checks, offers = [], []
    browser_by_retailer = {r: [c for c in browser_captures if c["retailer"] == r] for r in COMPETITORS}
    for domain in policy["online_domains"]:
        retailer = domain.removesuffix(".de")
        if retailer not in COMPETITORS:
            checks.append({"retailer": retailer, "status": "not_implemented"})
            continue
        adapter = COMPETITORS[retailer]
        captures = browser_by_retailer[retailer]
        if captures:
            urls = [c["source"] for c in captures]
            if supplied[retailer] and set(supplied[retailer]) != set(urls):
                raise SourceError("Browser sources must match the supplied competitor URLs")
            for capture in captures:
                for offer in capture["product"]["offers"]:
                    offer["assessment"] = assess(offer, product)
                    offers.append(offer)
            checks.append({"retailer": retailer, "scope": "supplied_browser_products",
                           "status": "browser_checked", "urls": urls,
                           "discovery_coverage": "not_verified"})
            continue
        check = {"retailer": retailer, "scope": "supplied_urls" if supplied[retailer] else "first_search_page"}
        try:
            if supplied[retailer]:
                urls = supplied[retailer]
            else:
                source, urls = adapter.discover(product["ean"])
                check["source"] = source
            check["urls"] = urls
            check["status"] = "checked" if urls else "no_search_results"
            failures = []
            for url in urls:
                try:
                    candidate = adapter.product(url)
                    for offer in candidate["offers"]:
                        offer["assessment"] = assess(offer, product)
                        offers.append(offer)
                except (ValueError, TypeError, KeyError, AttributeError, URLError, OSError) as exc:
                    failures.append({"source": url, **source_failure(exc)})
            if failures:
                check.update(status="partial", failures=failures)
        except (ValueError, TypeError, KeyError, AttributeError, URLError, OSError) as exc:
            check.update(source_failure(exc))
        checks.append(check)
    fallback_requests = []
    for check in checks:
        if check["status"] in ("blocked", "failed", "partial"):
            retailer = check["retailer"]
            fallback_requests.append({"retailer": retailer, "ean": product["ean"],
                                      "search_url": COMPETITORS[retailer].search_url(product["ean"]),
                                      "product_urls": check.get("urls", []),
                                      "reason": check["status"],
                                      "action": "manual_schema_research" if retailer == "joybuy" else "capture_browser_product_jsonld",
                                      "product_schema_verified": retailer != "joybuy"})
    rankable = [o for o in offers if o["assessment"]["status"] != "excluded"
                and o["price"] is not None and o["currency"] == "EUR"]
    rankable.sort(key=lambda o: (len(o["assessment"]["review_reasons"]), Decimal(o["price"]), o["url"]))
    return {"schema_version": 1, "status": "ok", "coverage": "partial",
            "observed_at": datetime.now(timezone.utc).isoformat(), "market_id": market_id,
            "product": product, "offers": offers, "retailer_checks": checks,
            "fallback_requests": fallback_requests,
            "policy_source": policy["policy_source"], "policy_checked_on": policy["checked_on"],
            "ranking_method": "fewest_unresolved_checks_then_item_price",
            "review_first": rankable[0] if rankable else None,
            "market_price_comparison": "unconfirmed",
            "local_offer_checks": "supplied_evidence_only" if review else "not_supplied",
            "review_evidence": review}
