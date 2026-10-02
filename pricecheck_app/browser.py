"""Validate bounded browser handoffs without storing page/profile data."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.http import SourceError
from .offers import HOSTS, parse_product
from .retailers import RETAILERS, product_url
from .observations import apply_observations

MAX_AGE_SECONDS = 900
MAX_BYTES = 128_000
PRODUCT_FIELDS = {"@type", "name", "url", "sku", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "brand", "offers"}
OFFER_FIELDS = {"@type", "url", "price", "priceCurrency", "itemCondition", "availability", "seller", "shippingDetails"}


def clean_product(product):
    if not isinstance(product, dict) or set(product) - PRODUCT_FIELDS:
        raise SourceError("Browser product contains unsupported fields")
    offers = product.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    if not offers or len(offers) > 12:
        raise SourceError("Browser capture requires 1 to 12 concrete offers")
    for offer in offers:
        if not isinstance(offer, dict) or set(offer) - OFFER_FIELDS:
            raise SourceError("Browser offer contains unsupported fields")
    # Nested data is passed only to the existing parser; raw data is never emitted.
    return product


def validate_capture(capture, now=None):
    if (not isinstance(capture, dict) or set(capture) not in
            ({"source", "observed_at", "product"}, {"source", "observed_at", "product", "observations"})):
        raise SourceError("Browser capture requires source, observed_at, product and optional observations")
    url = capture["source"]
    if not isinstance(url, str):
        raise SourceError("Invalid browser source")
    retailer = next((r for r in RETAILERS if urlsplit(url).hostname == HOSTS[r]), None)
    if retailer is None:
        raise SourceError("Browser capture retailer is unsupported")
    product_url(url, retailer)
    stamp = capture["observed_at"]
    try:
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SourceError("Browser observation time must be an ISO timestamp with timezone") from None
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    if not -60 <= age <= MAX_AGE_SECONDS:
        raise SourceError("Browser evidence is stale or dated in the future; recapture the page")
    product = clean_product(capture["product"])
    try:
        payload = json.dumps(product, ensure_ascii=False, allow_nan=False)
    except (ValueError, RecursionError):
        raise SourceError("Invalid browser product data") from None
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise SourceError("Browser evidence exceeds the size limit")
    # Escaping prevents HTMLParser interpreting a retailer string as script markup.
    html = '<script type="application/ld+json">' + payload.replace("<", "\\u003c") + '</script>'
    parsed = parse_product(html, url, retailer)
    if "observations" in capture:
        apply_observations(capture["observations"], parsed["offers"], observed)
    evidence_payload = json.dumps(capture, ensure_ascii=False, allow_nan=False)
    if len(evidence_payload.encode("utf-8")) > MAX_BYTES:
        raise SourceError("Browser evidence exceeds the size limit")
    digest = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()
    for offer in parsed["offers"]:
        offer.update(evidence="browser_jsonld", observed_at=observed.astimezone(timezone.utc).isoformat(),
                     evidence_sha256=digest, capture_trust="user_or_agent_supplied_not_authenticated")
    return {"retailer": retailer, "source": url, "product": parsed}


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError("Duplicate key in browser evidence")
        result[key] = value
    return result


def load_browser_evidence(paths):
    if len(paths) > 12 or paths.count("-") > 1:
        raise SourceError("At most 12 browser files and one stdin input are supported")
    captures = []
    sources = set()
    for path in paths:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise SourceError("Browser evidence exceeds the size limit")
        try:
            document = json.loads(raw, object_pairs_hook=reject_duplicates)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise SourceError("Invalid browser evidence JSON") from None
        if (not isinstance(document, dict) or set(document) != {"schema_version", "captures"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or not isinstance(document["captures"], list) or not document["captures"]):
            raise SourceError("Browser evidence must use schema_version 1 and nonempty captures")
        if len(captures) + len(document["captures"]) > 12:
            raise SourceError("At most 12 browser captures are supported")
        for capture in document["captures"]:
            normalized = validate_capture(capture)
            if normalized["source"] in sources:
                raise SourceError("Duplicate browser product source")
            sources.add(normalized["source"])
            captures.append(normalized)
    return captures
