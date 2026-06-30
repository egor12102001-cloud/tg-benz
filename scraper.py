"""
Scraper for gdebenz.ru — extracts fuel availability and prices by city.

gdebenz.ru is a crowdsourced map that loads station data via JSON API.
We use Playwright to intercept XHR/fetch responses and extract station data.

City pages follow the pattern: https://gdebenz.ru/<city-slug>/
Slugs are transliterated Russian city names, e.g. "Москва" → "moskva".
"""
import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional, Any

BASE_URL = "https://gdebenz.ru"
CHROMIUM_PATH = None  # None = Playwright auto-detects after `playwright install chromium`

FUEL_LABELS: dict[str, str] = {
    "ai92": "АИ-92", "petrol92": "АИ-92",
    "ai95": "АИ-95", "petrol95": "АИ-95",
    "ai98": "АИ-98", "petrol98": "АИ-98",
    "ai100": "АИ-100",
    "dt": "ДТ", "diesel": "ДТ",
    "gas": "Газ (LPG)", "lpg": "Газ (LPG)",
    "cng": "Газ (CNG)",
}

FUEL_STATUS_LABELS: dict = {
    "available": "✅ Есть",
    "yes": "✅ Есть",
    "queue": "🟡 Очередь",
    "limited": "🟠 Мало",
    "no": "❌ Нет",
    "none": "❌ Нет",
    "unknown": "❓",
    True: "✅ Есть",
    False: "❌ Нет",
    1: "✅ Есть",
    2: "🟡 Очередь",
    3: "🟠 Мало",
    4: "❌ Нет",
    0: "❓",
}

# Russian → Latin transliteration table for URL slugs
_TRANSLIT = str.maketrans({
    "а": "a",  "б": "b",  "в": "v",  "г": "g",  "д": "d",
    "е": "e",  "ё": "yo", "ж": "zh", "з": "z",  "и": "i",
    "й": "j",  "к": "k",  "л": "l",  "м": "m",  "н": "n",
    "о": "o",  "п": "p",  "р": "r",  "с": "s",  "т": "t",
    "у": "u",  "ф": "f",  "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch","ъ": "",   "ы": "y",  "ь": "",
    "э": "e",  "ю": "yu", "я": "ya",
    " ": "-",  "_": "-",
})


def city_to_slug(name: str) -> str:
    """Transliterate Russian city name to a URL slug."""
    return re.sub(r"-+", "-", name.lower().translate(_TRANSLIT).strip("-"))


@dataclass
class FuelStation:
    name: str
    address: str
    lat: float = 0.0
    lon: float = 0.0
    fuel_types: dict[str, str] = field(default_factory=dict)
    updated: str = ""


@dataclass
class CityFuelInfo:
    city: str
    city_slug: str
    stations: list[FuelStation] = field(default_factory=list)
    total: int = 0
    error: Optional[str] = None


# ─── Playwright ───────────────────────────────────────────────────────────────

def _launch_kwargs() -> dict:
    kwargs: dict = {"headless": True}
    if CHROMIUM_PATH:
        kwargs["executable_path"] = CHROMIUM_PATH
    kwargs["args"] = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    return kwargs


async def _intercept_api(url: str, timeout: int = 35000) -> tuple[str, list[dict]]:
    """
    Open `url` in headless Chromium, collect all JSON API responses,
    return (page_html, list_of_{url, data}).
    """
    from playwright.async_api import async_playwright

    api_responses: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(**_launch_kwargs())
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = await ctx.new_page()

        async def on_response(response):
            ct = response.headers.get("content-type", "")
            if "json" in ct and response.status == 200:
                try:
                    body = await response.json()
                    api_responses.append({"url": response.url, "data": body})
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout)
            await asyncio.sleep(2)
            html = await page.content()
        finally:
            await browser.close()

    return html, api_responses


# ─── Public API ───────────────────────────────────────────────────────────────

async def get_fuel_info(city_name: str) -> CityFuelInfo:
    """
    Main entry point. Accepts a Russian city name (or a slug).
    Tries multiple slug variants, returns the first successful result.
    """
    slug = city_to_slug(city_name)
    variants = _slug_variants(slug)

    for variant in variants:
        url = f"{BASE_URL}/{variant}/"
        try:
            html, api_responses = await _intercept_api(url)
        except Exception as e:
            return CityFuelInfo(city=city_name, city_slug=variant, error=str(e))

        # Extract city name from page
        display_name = _extract_city_name(html, city_name)

        # Parse stations from intercepted API calls
        stations = _stations_from_responses(api_responses)
        if stations:
            return CityFuelInfo(
                city=display_name,
                city_slug=variant,
                stations=stations,
                total=len(stations),
            )

        # If page actually loaded (has title / h1) but no stations, stop trying variants
        if display_name and display_name != city_name:
            return CityFuelInfo(
                city=display_name,
                city_slug=variant,
                stations=[],
                total=0,
            )

    return CityFuelInfo(
        city=city_name,
        city_slug=slug,
        stations=[],
        total=0,
        error="Город не найден на сайте gdebenz.ru",
    )


async def search_city(query: str) -> list[dict]:
    """
    Return candidate city dicts [{name, slug, url}] for the given query.
    We generate slug variants and return them as candidates — no pre-fetching needed.
    """
    slug = city_to_slug(query.strip())
    variants = _slug_variants(slug)
    return [
        {"name": query.strip(), "slug": v, "url": f"{BASE_URL}/{v}/"}
        for v in variants
    ]


async def get_cities() -> list[dict]:
    """
    Try to load the main page and extract city links from HTML/API.
    Returns an empty list if the site doesn't expose a city list.
    """
    try:
        html, api_responses = await _intercept_api(BASE_URL)
    except Exception:
        return []

    for resp in api_responses:
        cities = _cities_from_json(resp["data"])
        if cities:
            return cities

    return _cities_from_html(html)


# ─── Slug helpers ─────────────────────────────────────────────────────────────

def _slug_variants(slug: str) -> list[str]:
    """Generate plausible slug variants for a Russian city name."""
    variants = [slug]
    # Some cities have common alternative transliterations
    alts = {
        "moskva": ["moscow"],
        "saint-peterburg": ["spb", "sankt-peterburg", "saint-petersburg"],
        "nizhni-novgorod": ["nizhnij-novgorod", "nn"],
        "rostov-na-donu": ["rostov"],
        "krasnodar": ["krd"],
    }
    for v in list(variants):
        variants += alts.get(v, [])
    return list(dict.fromkeys(variants))


# ─── Parsers ──────────────────────────────────────────────────────────────────

def _stations_from_responses(api_responses: list[dict]) -> list[FuelStation]:
    for resp in api_responses:
        stations = _extract_stations_from_json(resp["data"])
        if stations:
            return stations
    return []


def _extract_city_name(html: str, fallback: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    title = soup.find("title")
    if title:
        t = title.get_text(strip=True)
        return t.split("—")[0].split("|")[0].strip()
    return fallback


def _extract_stations_from_json(data: Any) -> list[FuelStation]:
    stations: list[FuelStation] = []

    if isinstance(data, list):
        for item in data:
            s = _parse_station(item)
            if s:
                stations.append(s)
    elif isinstance(data, dict):
        # GeoJSON FeatureCollection
        if data.get("type") == "FeatureCollection":
            for feature in data.get("features", []):
                s = _parse_geojson_feature(feature)
                if s:
                    stations.append(s)
            return stations
        # Nested list under common keys
        for key in ("stations", "azs", "items", "data", "results", "list"):
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    s = _parse_station(item)
                    if s:
                        stations.append(s)
                if stations:
                    return stations

    return stations


def _parse_station(item: Any) -> Optional[FuelStation]:
    if not isinstance(item, dict):
        return None

    name = (
        item.get("name") or item.get("title") or
        item.get("brand") or item.get("network") or ""
    )
    address = (
        item.get("address") or item.get("addr") or
        item.get("street") or item.get("location") or ""
    )
    lat = float(item.get("lat") or item.get("latitude") or 0)
    lon = float(item.get("lon") or item.get("lng") or item.get("longitude") or 0)
    updated = str(
        item.get("updated_at") or item.get("updated") or
        item.get("time") or item.get("timestamp") or ""
    )

    fuel_types: dict[str, str] = {}

    # Nested list: [{type, status, price}, ...]
    for fuel_key in ("fuels", "fuel", "prices", "types"):
        val = item.get(fuel_key)
        if isinstance(val, list):
            for f in val:
                if not isinstance(f, dict):
                    continue
                ft = (f.get("type") or f.get("name") or f.get("fuel_type") or "").lower()
                label = FUEL_LABELS.get(ft, ft.upper()) if ft else ""
                if not label:
                    continue
                status_raw = f.get("status") or f.get("available") or f.get("state")
                price = f.get("price") or f.get("cost") or ""
                status_str = FUEL_STATUS_LABELS.get(status_raw, "")
                if price and status_str:
                    fuel_types[label] = f"{status_str} · {price} ₽/л"
                elif price:
                    fuel_types[label] = f"{price} ₽/л"
                elif status_str:
                    fuel_types[label] = status_str
            break
        elif isinstance(val, dict):
            for ft, fval in val.items():
                label = FUEL_LABELS.get(ft.lower(), ft.upper())
                if isinstance(fval, dict):
                    status_raw = fval.get("status") or fval.get("available")
                    price = fval.get("price") or ""
                    status_str = FUEL_STATUS_LABELS.get(status_raw, "")
                    if price and status_str:
                        fuel_types[label] = f"{status_str} · {price} ₽/л"
                    elif price:
                        fuel_types[label] = f"{price} ₽/л"
                    elif status_str:
                        fuel_types[label] = status_str
                elif fval is not None:
                    fuel_types[label] = str(fval)
            break

    # Flat fields: ai92_status, price_ai95, ai95=54.5, etc.
    for k, v in item.items():
        k_low = k.lower()
        m = re.match(r"(ai\d+|dt|diesel|gas|lpg|cng|petrol\d*)([-_]?(status|price|available))?$", k_low)
        if not m or v is None:
            continue
        ft = m.group(1)
        suffix = (m.group(3) or "").lower()
        label = FUEL_LABELS.get(ft, ft.upper())
        if suffix == "price" or (not suffix and isinstance(v, (int, float))):
            fuel_types.setdefault(label, f"{v} ₽/л")
        elif suffix in ("status", "available"):
            s = FUEL_STATUS_LABELS.get(v, str(v))
            if label in fuel_types:
                fuel_types[label] = f"{s} · {fuel_types[label]}"
            else:
                fuel_types[label] = s

    if not name and not address and not fuel_types:
        return None

    return FuelStation(
        name=name, address=address,
        lat=lat, lon=lon,
        fuel_types=fuel_types, updated=updated,
    )


def _parse_geojson_feature(feature: dict) -> Optional[FuelStation]:
    props = feature.get("properties", {})
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [0, 0])
    return _parse_station({
        **props,
        "lon": coords[0] if len(coords) > 0 else 0,
        "lat": coords[1] if len(coords) > 1 else 0,
    })


def _cities_from_json(data: Any) -> list[dict]:
    cities: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("city")
                slug = item.get("slug") or item.get("code") or str(item.get("id", ""))
                if name and slug:
                    cities.append({"name": str(name), "slug": str(slug), "url": f"{BASE_URL}/{slug}/"})
    elif isinstance(data, dict):
        for key in ("cities", "regions", "items", "data", "results"):
            if key in data and isinstance(data[key], list):
                cities = _cities_from_json(data[key])
                if cities:
                    return cities
    return cities


def _cities_from_html(html: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    cities: list[dict] = []
    seen: set[str] = set()
    skip = {"about", "api", "map", "help", "faq", "login", "register", "privacy", "terms"}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if re.match(r"^/[a-z][a-z0-9\-]{2,}/?$", href) and text and len(text) > 2:
            slug = href.strip("/")
            if slug in skip or slug in seen:
                continue
            seen.add(slug)
            cities.append({"name": text, "slug": slug, "url": BASE_URL + href})
    return cities
