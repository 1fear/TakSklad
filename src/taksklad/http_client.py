import ssl
import urllib.parse
import urllib.request

import certifi

from .utils import normalize_text


HTTPS_CONTEXT = None


def get_https_context():
    global HTTPS_CONTEXT
    if HTTPS_CONTEXT is None:
        HTTPS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return HTTPS_CONTEXT


def open_https_url(request, timeout):
    url = request.full_url if isinstance(request, urllib.request.Request) else normalize_text(request)
    kwargs = {"timeout": timeout}
    if urllib.parse.urlparse(url).scheme.lower() == "https":
        kwargs["context"] = get_https_context()
    return urllib.request.urlopen(request, **kwargs)
