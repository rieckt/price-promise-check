"""A shared offer contract, parsed from product-scoped JSON-LD."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .adapters.http import SourceError, validate_url
from .adapters.mediamarkt import Scripts, money
from .retailers import RETAILERS

HOSTS = {"mediamarkt": "www.mediamarkt.de", **{name: r["host"] for name, r in RETAILERS.items()}}
DIRECT_SELLERS = {"mediamarkt": {"mediamarkt", "mediamarkt ecommerce gmbh"},
                  "alternate": {"alternate gmbh"},
                  "galaxus": {"galaxus", "galaxus deutschland gmbh"}}
DIRECT_SELLERS.update({name: {name, name + ".de"} for name in RETAILERS if name not in DIRECT_SELLERS})


def gtin(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d{8}|\d{12}|\d{13}|\d{14})", value, re.ASCII):
        raise SourceError("Missing or invalid GTIN; exact comparison is unavailable")
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(value[:-1])))
    if (total + int(value[-1])) % 10:
        raise SourceError("GTIN checksum is invalid")
    return value.zfill(14)


def schema_name(value):
    return value.rsplit("/", 1)[-1] if isinstance(value, str) else None


def nodes(value):
    if isinstance(value, list):
        for child in value:
            yield from nodes(child)
    elif isinstance(value, dict):
        yield value
        for field in ("@graph", "object"):
            yield from nodes(value.get(field))


def delivery_window(details):
    if not isinstance(details, dict):
        return None
    country = details.get("shippingDestination", {}).get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    if country != "DE":
        return None
    time = details.get("deliveryTime")
    if not isinstance(time, dict):
        return None
    ranges = []
    for field in ("handlingTime", "transitTime"):
        interval = time.get(field)
        if not isinstance(interval, dict) or interval.get("unitCode") != "DAY":
            return None
        low, high = interval.get("minValue"), interval.get("maxValue")
        if (type(low) not in (int, float) or type(high) not in (int, float)
                or not 0 <= low <= high <= 365):
            return None
        ranges.append((low, high))
    # General country-level metadata is not an address-specific delivery promise.
    return {"min_days": sum(r[0] for r in ranges), "max_days": sum(r[1] for r in ranges),
            "scope": "country_DE", "calendar_basis": "unspecified"}


@dataclass(frozen=True)
class Offer:
    retailer: str
    product_id: str
    name: str
    brand: str | None
    gtin: str
    url: str
    price: str | None
    currency: str | None
    shipping: str | None
    seller: str | None
    seller_kind: str
    condition: str
    availability: str
    delivery: dict | None
    evidence: str = "product_jsonld"

    def __post_init__(self):
        for field in ("name", "brand", "seller", "product_id", "currency"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str)
                                      or any(ord(c) < 32 for c in value)):
                raise SourceError("Invalid offer text field")
        validate_url(self.url, HOSTS[self.retailer])
        if gtin(self.gtin) != self.gtin:
            raise SourceError("Offer GTIN must be normalized")

    def to_dict(self):
        return asdict(self)


def parse_product(html, url, retailer):
    host = HOSTS[retailer]
    validate_url(url, host)
    parser = Scripts()
    parser.feed(html)
    products = []
    for attrs, content in parser.scripts:
        if attrs.get("type") == "application/ld+json":
            for node in nodes(json.loads(content)):
                if node.get("@type") in ("Product", "ProductGroup"):
                    offers = node.get("offers")
                    offers = offers if isinstance(offers, list) else [offers]
                    urls = [node.get("url")] + [o.get("url") for o in offers if isinstance(o, dict)]
                    if any(isinstance(u, str) and urlsplit(u)._replace(query="", fragment="")
                           == urlsplit(url)._replace(query="", fragment="") for u in urls):
                        products.append(node)
    if len(products) != 1:
        raise SourceError("Product JSON-LD is missing or ambiguous for this URL")
    product = products[0]
    ean = next((product.get(k) for k in ("gtin", "gtin13", "gtin14", "gtin12", "gtin8")
                if product.get(k) is not None), None)
    code = gtin(ean)
    name = product.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SourceError("Product name is missing")
    brand = product.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    raw_offers = product.get("offers")
    raw_offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
    if not raw_offers:
        raise SourceError("Concrete offer missing")
    results = []
    for raw in raw_offers:
        if not isinstance(raw, dict) or raw.get("@type") != "Offer":
            raise SourceError("Concrete offer missing; aggregate prices cannot be compared")
        offer_url = raw.get("url", url)
        validate_url(offer_url, host)
        if urlsplit(offer_url).path != urlsplit(url).path:
            raise SourceError("Offer belongs to a different product URL")
        seller = raw.get("seller")
        seller = seller.get("name") if isinstance(seller, dict) else None
        if seller is not None and not isinstance(seller, str):
            raise SourceError("Invalid seller name")
        kind = ("unknown" if not seller else "direct" if seller.strip().casefold()
                in DIRECT_SELLERS[retailer] else "marketplace" if retailer in
                ("mediamarkt", "amazon", "otto", "galaxus") else "unknown")
        details = raw.get("shippingDetails")
        if isinstance(details, list):
            details = details[0] if len(details) == 1 else None
        rate = details.get("shippingRate") if isinstance(details, dict) else None
        shipping = money(rate.get("value")) if isinstance(rate, dict) and rate.get("currency") == raw.get("priceCurrency") else None
        condition = {"NewCondition": "new", "UsedCondition": "used",
                     "RefurbishedCondition": "refurbished"}.get(schema_name(raw.get("itemCondition")), "unknown")
        availability = {"InStock": "available", "OutOfStock": "unavailable",
                        "SoldOut": "unavailable", "Discontinued": "unavailable"}.get(schema_name(raw.get("availability")), "unknown")
        results.append(Offer(retailer, str(product.get("sku") or ""), name, brand, code,
                             offer_url, money(raw.get("price")), raw.get("priceCurrency"),
                             shipping, seller, kind, condition, availability,
                             delivery_window(details)).to_dict())
    return {"name": name, "brand": brand, "gtin": code, "ean": ean, "url": url, "offers": results}
