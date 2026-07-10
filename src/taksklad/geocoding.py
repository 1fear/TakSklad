import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .secret_store import GEOCODER_API_KEY_SECRET, SecretStoreError, load_secret
from .utils import normalize_coordinates, normalize_text


COUNTRY_PREFIXES = ("узбекистан", "uzbekistan", "o'zbekiston", "oʻzbekiston")


def load_yandex_geocoder_key():
    try:
        return normalize_text(load_secret(GEOCODER_API_KEY_SECRET))
    except SecretStoreError:
        logging.warning("Безопасное хранилище ключа Геокодера недоступно")
        return ""


def reverse_geocode_yandex(coords, cache=None):
    normalized_coords = normalize_coordinates(coords)
    if not normalized_coords:
        return None, "некорректные координаты"

    if cache is not None and normalized_coords in cache:
        return cache[normalized_coords]

    api_key = load_yandex_geocoder_key()
    if not api_key:
        result = (None, "не указан ключ Яндекс Геокодера")
        if cache is not None:
            cache[normalized_coords] = result
        return result

    params = {
        "apikey": api_key,
        "geocode": normalized_coords,
        "format": "json",
        "lang": "ru_RU",
        "sco": "latlong",
        "results": "1",
        "kind": "house",
    }
    url = "https://geocode-maps.yandex.ru/v1/?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        result = (None, f"HTTP {exc.code}: {body}")
        if cache is not None:
            cache[normalized_coords] = result
        return result
    except Exception as exc:
        result = (None, str(exc))
        if cache is not None:
            cache[normalized_coords] = result
        return result

    members = data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
    if not members:
        result = (None, "адрес не найден")
        if cache is not None:
            cache[normalized_coords] = result
        return result

    obj = members[0].get("GeoObject", {})
    meta = obj.get("metaDataProperty", {}).get("GeocoderMetaData", {})
    address = clean_geocoded_address(meta.get("text") or obj.get("name"))
    if not address:
        result = (None, "пустой адрес в ответе Яндекса")
    else:
        result = (address, "")

    if cache is not None:
        cache[normalized_coords] = result
    return result


def clean_geocoded_address(value):
    text = normalize_text(value)
    lowered = text.lower()
    for prefix in COUNTRY_PREFIXES:
        if lowered == prefix:
            return ""
        if lowered.startswith(prefix + ","):
            return text[len(prefix):].lstrip(" ,")
    return text
