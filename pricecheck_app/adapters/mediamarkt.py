"""Read public MediaMarkt search pages without executing their JavaScript."""
import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from .http import SourceError, fetch as fetch_public, validate_url as validate_public_url


def validate_url(url):
    return validate_public_url(url, "www.mediamarkt.de")


def fetch(url):
    return fetch_public(url, "www.mediamarkt.de")


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = [dict(attrs), []]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append((self.current[0], "".join(self.current[1])))
            self.current = None


def page_data(html):
    parser = Scripts()
    parser.feed(html)
    state = None
    item_list = None
    for attrs, script in parser.scripts:
        if attrs.get("type") == "application/ld+json":
            value = json.loads(script)
            if isinstance(value, dict) and value.get("@type") == "ItemList":
                item_list = value.get("itemListElement")
        match = re.fullmatch(r"\s*window\.__PRELOADED_STATE__\s*=\s*(.*?)\s*;?\s*",
                             script, re.DOTALL)
        if match:
            # SSR contains undefined values. Preserve strings, replace only tokens.
            raw = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b',
                         lambda m: "null" if m.group() == "undefined" else m.group(),
                         match.group(1))
            state = json.loads(raw)
    if not isinstance(state, dict) or not isinstance(state.get("apolloState"), dict):
        raise SourceError("MediaMarkt page schema changed or access was blocked")
    if not isinstance(item_list, list):
        raise SourceError("Search result list missing; cannot claim zero results")
    cache = state["apolloState"]
    root = cache.get("ROOT_QUERY", {})
    searches = [v for k, v in root.items() if k.startswith("searchV4:")]
    if len(searches) != 1 or not isinstance(searches[0], dict):
        raise SourceError("Search metadata missing or ambiguous")
    total = searches[0].get("totalProducts")
    if type(total) is not int or total < 0:
        raise SourceError("Search result count missing or invalid")
    if total == 0:
        # JSON-LD may contain fallback recommendations despite zero search hits.
        return [], cache
    if not item_list:
        raise SourceError("Search reports hits but no result items were parsed")
    if len(item_list) > total:
        raise SourceError("Result list exceeds search count; cannot distinguish suggestions")
    return item_list, cache


def money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return format(amount.quantize(Decimal("0.01")), "f")
    except (InvalidOperation, ValueError):
        raise SourceError("Invalid price in retailer response")


def parse_search(html):
    items, cache = page_data(html)
    features = {}
    for value in cache.values():
        if isinstance(value, dict) and value.get("__typename") in (
                "CofrCoreFeature", "CofrPriceFeature", "CofrOnlineStatusFeature",
                "CofrRefurbishedGoodsFeature"):
            features.setdefault(value.get("id"), {})[value["__typename"]] = value
    results = []
    seen = set()
    for entry in items:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise SourceError("Invalid product in search result list")
        url = validate_url(item.get("url", ""))
        match = re.search(r"-(\d+)\.html$", urlsplit(url).path)
        if not match:
            raise SourceError("Product ID missing in result URL")
        product_id = match.group(1)
        if product_id in seen:
            continue
        seen.add(product_id)
        data = features.get("Media:de:" + product_id, {})
        core = data.get("CofrCoreFeature", {})
        price = data.get("CofrPriceFeature", {})
        status = data.get("CofrOnlineStatusFeature", {})
        refurbished = data.get("CofrRefurbishedGoodsFeature", {})
        seller = price.get("marketplaceSeller")
        marketplace = price.get("isProductOfTypeMarketplace")
        if marketplace is True:
            seller_kind = "marketplace"
            seller_name = seller.get("sellerName") if isinstance(seller, dict) else None
        elif marketplace is False and "marketplaceSeller" in price and seller is None:
            seller_kind, seller_name = "direct", "MediaMarkt"
        else:
            seller_kind, seller_name = "unknown", None
        available = status.get("isAvailableAndBuyable")
        availability = "available" if available is True else (
            "unavailable" if available is False else "unknown")
        offers = item.get("offers", {})
        if not isinstance(offers, dict):
            raise SourceError("Invalid search offer")
        base_price = price.get("price") or {}
        promotions = []
        for field, audience in (("promoPrice", "public"), ("loyaltyPromoPrice", "member")):
            promo = price.get(field)
            if isinstance(promo, dict) and promo.get("amount") is not None:
                promotions.append({"audience": audience, "price": money(promo["amount"]),
                                   "shipping": money(promo.get("shippingCost")),
                                   "eligibility": "not_checked"})
        condition = "unknown"
        active_offer_id = core.get("relevantOfferId")
        if (active_offer_id is not None and isinstance(refurbished.get("offer"), dict)
                and str(refurbished["offer"].get("offerId")) == str(active_offer_id)):
            condition = refurbished["offer"].get("conditionType") or "refurbished"
        results.append({"retailer": "mediamarkt", "product_id": product_id,
                        "name": item["name"], "ean": core.get("ean"), "url": url,
                        "price": money(base_price.get("amount", offers.get("price"))),
                        "currency": price.get("currency", offers.get("priceCurrency")),
                        "shipping": money(base_price.get("shippingCost")),
                        "seller_kind": seller_kind, "seller": seller_name,
                        "condition": condition, "active_offer_id": active_offer_id,
                        "online_availability": availability,
                        "online_status_raw": status.get("onlineStatus"),
                        "market_availability": "unknown", "promotions": promotions,
                        "price_promise": "not_checked"})
    return results


def matches_query(product, query):
    # Literal token matching is a candidate filter, not proof of product identity.
    terms = set(re.findall(r"[^\W_]+", query.casefold()))
    text = " ".join(str(product.get(k) or "") for k in ("name", "ean", "product_id"))
    tokens = set(re.findall(r"[^\W_]+", text.casefold()))
    return bool(terms) and terms <= tokens


class MediaMarkt:
    def product(self, url):
        from ..offers import parse_product
        validate_url(url)
        if not re.fullmatch(r"/de/product/[^/]+-\d+\.html", urlsplit(url).path):
            raise SourceError("Supply a MediaMarkt product URL")
        return parse_product(fetch(url), url, "mediamarkt")

    def search(self, query):
        url = "https://www.mediamarkt.de/de/search.html?" + urlencode({"query": query})
        products = parse_search(fetch(url))
        for product in products:
            product["query_token_match"] = matches_query(product, query)
            product["identity_match"] = "unverified"
        return url, products
