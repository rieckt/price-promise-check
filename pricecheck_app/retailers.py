"""Approved retailer routes; absent discovery templates require browser search."""
from urllib.parse import quote, urlsplit
import re

from .adapters.http import SourceError, validate_url

RETAILERS = {
    "alternate": {"host": "www.alternate.de", "path": r"/[^?#]+/html/product/\d+", "search": "https://www.alternate.de/listing.xhtml?q={query}"},
    "galaxus": {"host": "www.galaxus.de", "path": r"/(?:de|en)/s\d+/product/[^/?#]+-\d+", "search": "https://www.galaxus.de/search?q={query}"},
    "amazon": {"host": "www.amazon.de", "path": r"/(?:[^/]+/)?dp/[A-Z0-9]{10}/?", "search": "https://www.amazon.de/s?k={query}"},
    "otto": {"host": "www.otto.de", "path": r"/p/[^/?#]+/", "search": "https://www.otto.de/suche/{query}/"},
    "cyberport": {"host": "www.cyberport.de", "path": r"/[^?#]+/pdp/[^/]+/[^/]+\.html", "search": "https://www.cyberport.de/tools/search-results.html?autosuggest=false&q={query}"},
    "notebooksbilliger": {"host": "www.notebooksbilliger.de", "path": r"/[^/?#]+(?:\+|%2[Bb])\d{5,}/?", "search": None},
    "coolblue": {"host": "www.coolblue.de", "path": r"/produkt/\d+/[^/]+\.html", "search": "https://www.coolblue.de/suchen?query={query}"},
    "expert": {"host": "www.expert.de", "path": r"/shop/unsere-produkte/[^?#]+/\d+-[^/]+\.html", "search": "https://www.expert.de/shop/suche?q={query}"},
    "joybuy": {"host": "www.joybuy.de", "path": None, "search": None, "landing": "https://www.joybuy.de/cms/home"},
}


def discovery_url(retailer, query):
    route = RETAILERS[retailer]
    if route["search"]:
        return route["search"].format(query=quote(query, safe=""))
    return route.get("landing", "https://" + route["host"] + "/")


def product_url(url, retailer):
    route = RETAILERS[retailer]
    validate_url(url, route["host"])
    parts = urlsplit(url)
    if not route["path"]:
        raise SourceError("Retailer product URL schema is not verified yet")
    if parts.query or parts.fragment or not re.fullmatch(route["path"], parts.path):
        raise SourceError("Supply a canonical supported retailer product URL")
    return url
