"""Bounded public HTTPS reads with retailer-scoped redirects."""
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceError(ValueError):
    pass


def validate_url(url, host):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise SourceError("Invalid retailer URL")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != host or parts.username
            or parts.password or parts.port not in (None, 443)):
        raise SourceError("Only HTTPS URLs on " + host + " are allowed")
    return url


def fetch(url, host):
    validate_url(url, host)

    class SafeRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate_url(newurl, host)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (personal pricecheck/0.1)",
                                    "Accept-Language": "de-DE,de;q=0.9"})
    with build_opener(SafeRedirect()).open(request, timeout=20) as response:
        validate_url(response.url, host)
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise SourceError("Page exceeds the response size limit")
        return data.decode("utf-8")
