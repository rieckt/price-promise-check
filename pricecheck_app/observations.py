"""Bounded visible-page observations tied to an existing exact offer."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .adapters.http import SourceError
from .offers import DIRECT_SELLERS


def text(value, limit=300):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value) or "<" in value or ">" in value):
        raise SourceError("Invalid observation text")
    return value


def apply_observations(observations, offers, observed):
    if not isinstance(observations, list) or not 1 <= len(observations) <= 12:
        raise SourceError("Supply 1 to 12 visible offer observations")
    seen = set()
    for entry in observations:
        if not isinstance(entry, dict) or set(entry) != {"offer_price", "seller", "delivery"}:
            raise SourceError("Observation requires offer_price, seller and delivery")
        try:
            price = Decimal(entry["offer_price"])
            if not isinstance(entry["offer_price"], str) or not price.is_finite() or price < 0:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            raise SourceError("Invalid observed offer price") from None
        matching = [o for o in offers if o["price"] is not None and Decimal(o["price"]) == price]
        if len(matching) != 1 or price in seen:
            raise SourceError("Observation price must select one unique captured offer")
        seen.add(price)
        offer = matching[0]
        seller, delivery = entry["seller"], entry["delivery"]
        if seller is None and delivery is None:
            raise SourceError("Observation has no evidence")
        if seller is not None:
            if not isinstance(seller, dict) or set(seller) != {"name", "quote"}:
                raise SourceError("Seller observation requires name and quote")
            name, quote = text(seller["name"], 100), text(seller["quote"])
            if name.casefold() not in quote.casefold():
                raise SourceError("Seller quote must name the observed seller")
            if offer["seller"] and offer["seller"].casefold() != name.casefold():
                raise SourceError("Visible seller conflicts with captured offer")
            retailer = offer["retailer"]
            offer["seller"] = name
            offer["seller_kind"] = ("direct" if name.strip().casefold() in DIRECT_SELLERS[retailer]
                                    else "marketplace" if retailer in ("amazon", "otto", "galaxus") else "unknown")
        if delivery is not None:
            if (not isinstance(delivery, dict) or set(delivery) != {"latest_date", "quote", "scope"}
                    or delivery["scope"] != "country_DE"):
                raise SourceError("Delivery observation requires a country_DE date and quote")
            text(delivery["quote"])
            try:
                latest = date.fromisoformat(delivery["latest_date"])
                days = (latest - observed.astimezone(ZoneInfo("Europe/Berlin")).date()).days
                if not 0 <= days <= 365:
                    raise ValueError
            except (ValueError, TypeError):
                raise SourceError("Invalid observed delivery date") from None
            # A visible advertised date is useful evidence, not an address guarantee.
            offer["advertised_delivery"] = {**delivery, "calendar_days": days,
                                             "address_guarantee": False}
        offer["visible_observation"] = entry


def validate_review(document, product, market_id, now=None):
    """Retain manually supplied public evidence without certifying acceptance."""
    if (not isinstance(document, dict) or set(document) !=
            {"schema_version", "source", "gtin", "observed_at", "market", "local_offers"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1):
        raise SourceError("Invalid review evidence envelope")
    from .offers import gtin
    if document["source"] != product["url"] or gtin(document["gtin"]) != product["gtin"]:
        raise SourceError("Review evidence must match the selected MediaMarkt product")
    now = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(document["observed_at"].replace("Z", "+00:00"))
        if observed.utcoffset() is None or not -60 <= (now - observed).total_seconds() <= 900:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SourceError("Review evidence must be fresh and timezone-aware") from None
    market = document["market"]
    if market is not None:
        if (not isinstance(market, dict) or set(market) != {"id", "pickup", "stock", "price", "evidence_kind"}
                or market["id"] != market_id or market_id is None
                or market["pickup"] not in ("available_today", "unavailable", "unknown")
                or market["stock"] not in ("confirmed_in_store", "unavailable", "unknown")
                or market["evidence_kind"] not in ("selected_market_ui", "in_store_display")):
            raise SourceError("Invalid selected-market evidence")
        if market["price"] is not None:
            if market["evidence_kind"] != "in_store_display":
                raise SourceError("Online price cannot establish physical market price")
            checked_price(market["price"])
        if market["stock"] == "confirmed_in_store" and market["evidence_kind"] != "in_store_display":
            raise SourceError("Pickup availability alone cannot establish physical stock")
    local = document["local_offers"]
    if not isinstance(local, list) or len(local) > 12:
        raise SourceError("At most 12 local offers are supported")
    from .locations import catalogue
    catalog = catalogue()
    shops = {s["name"]: s for s in catalog["local_candidates"]} if (catalog.get("market") or {}).get("id") == market_id else {}
    results = []
    today = now.astimezone(ZoneInfo("Europe/Berlin")).date()
    for entry in local:
        if (not isinstance(entry, dict) or set(entry) !=
                {"shop", "gtin", "price", "currency", "valid_from", "valid_until", "evidence_kind", "source"}
                or entry["shop"] not in shops or entry["currency"] != "EUR"
                or entry["evidence_kind"] not in ("flyer", "printed_advertisement")):
            raise SourceError("Local offers require a catalogued shop and flyer or printed advertisement")
        checked_price(entry["price"])
        if gtin(entry["gtin"]) != product["gtin"]:
            raise SourceError("Local offer GTIN does not match")
        source = urlsplit(text(entry["source"], 2048))
        if (source.scheme != "https" or not source.hostname or source.username or source.password
                or source.query or source.fragment):
            raise SourceError("Supply a public HTTPS evidence URL without private parameters")
        try:
            start, end = date.fromisoformat(entry["valid_from"]), date.fromisoformat(entry["valid_until"])
            if not start <= today <= end:
                raise ValueError
        except (ValueError, TypeError):
            raise SourceError("Local advertisement is not currently valid") from None
        shop = shops[entry["shop"]]
        reasons = ["physical_stock_unconfirmed", "advertisement_authenticity_unconfirmed",
                   "membership_and_exclusions_unconfirmed", "market_acceptance_unconfirmed"]
        results.append({**entry, "distance_km": shop["distance_km"],
                        "radius_assessment": shop["radius_assessment"],
                        "status": "excluded" if not shop["within_30km_airline"] else "needs_review",
                        "excluded_reasons": ["outside_radius"] if not shop["within_30km_airline"] else [],
                        "review_reasons": reasons})
    return {"market": market, "local_offers": results, "observed_at": document["observed_at"],
            "capture_trust": "user_or_agent_supplied_not_authenticated"}


def checked_price(value):
    try:
        if not isinstance(value, str) or not Decimal(value).is_finite() or Decimal(value) < 0:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise SourceError("Invalid evidence price") from None


def load_review(path):
    from .browser import MAX_BYTES, reject_duplicates
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SourceError("Review evidence exceeds the size limit")
    try:
        return json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise SourceError("Invalid review evidence JSON") from None
