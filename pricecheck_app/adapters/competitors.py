"""First-page GTIN discovery and concrete competitor offer reads."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .http import SourceError, fetch
from ..offers import HOSTS, parse_product
from ..retailers import RETAILERS, discovery_url, product_url


class ProductLinks(HTMLParser):
    def __init__(self, retailer):
        super().__init__()
        self.retailer = retailer
        self.urls = []
        self.empty_notice = []
        self.in_notice = False

    def handle_data(self, text):
        if self.in_notice:
            self.empty_notice.append(text)

    def handle_endtag(self, tag):
        if tag == "div":
            self.in_notice = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and {"alert", "alert-info"} <= set(attrs.get("class", "").split()):
            self.in_notice = True
        if tag != "a" or not attrs.get("href"):
            return
        href = attrs["href"]
        if self.retailer == "alternate":
            if "productBox" not in attrs.get("class", "").split():
                return
            pattern = r"https://www\.alternate\.de/[^?#]+/html/product/\d+"
        else:
            href = urljoin("https://" + RETAILERS[self.retailer]["host"], href)
            try:
                product_url(href, self.retailer)
            except ValueError:
                return
            pattern = r"https://.*"
        if re.fullmatch(pattern, href) and href not in self.urls:
            self.urls.append(href)


class Competitor:
    def __init__(self, retailer):
        self.retailer = retailer
        self.host = HOSTS[retailer]

    def product(self, url):
        product_url(url, self.retailer)
        return parse_product(fetch(url, self.host), url, self.retailer)

    def search_url(self, ean):
        return discovery_url(self.retailer, ean)

    def discover(self, ean):
        source = self.search_url(ean)
        if RETAILERS[self.retailer]["search"] is None:
            raise SourceError("Public discovery schema is unverified; browser search is required")
        html = fetch(source, self.host)
        parser = ProductLinks(self.retailer)
        parser.feed(html)
        if not parser.urls:
            text = " ".join(parser.empty_notice)
            if self.retailer == "alternate" and "Ihre Suche erbrachte leider keine Ergebnisse." in text:
                return source, []
            raise SourceError("No product links parsed; absence of offers is unconfirmed")
        if len(parser.urls) > 12:
            raise SourceError("Search is too broad; supply a concrete competitor URL")
        return source, parser.urls


COMPETITORS = {name: Competitor(name) for name in RETAILERS}
