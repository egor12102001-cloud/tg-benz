"""
Scraper for gdebenz.ru — extracts fuel availability and prices by city.

The site is a crowdsourced map (карта АЗС) that loads station data via JSON API.
We use Playwright to intercept XHR/fetch calls and extract the API endpoints,
then query those APIs directly.
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Any

BASE_URL = "https://gdebenz.ru"
CHROMIUM_PATH = None  # None = Playwright finds Chromium automatically

# Fuel type labels
FUEL_LABELS = {
    "ai92": "АИ-92",
    "ai95": "АИ-95",
    "ai98": "АИ-98",
    "ai100": "АИ-100",
    "dt": "ДТ (дизель)",
    "gas": "Газ (LPG)",
    "diesel": "ДТ (дизель)",
    "petrol92": "АИ-92",
    "petrol95": "АИ-95",
    "petrol98": "АИ-98",
}

FUEL_STATUS_LABELS = {
    "available": "✅ Есть",
    "queue": "🟡 Очередь",
    "limited": "🟠 Мало",
    "none": "❌ Нет",
    "unknown": "❓ Неизвестно",
    1: "✅ Есть",
    2: "🟡 Очередь",
    3: "🟠 Мало",
    4: "❌ Нет",
}


@dataclass
class FuelStation:
    name: str
    address: str
    lat: float = 0.0
    lon: float = 0.0
    fuel_types: dict[str, str] = field(default_factory=dict)   # fuel -> "✅ Есть / цена"
    status: str = ""
    updated: str = ""


@dataclass
class CityFuelInfo:
    city: str
    city_slug: str
    stations: list[FuelStation] = field(default_factory=list)
    total: int = 0
    error: Optional[str] = None


# ─── Playwright helpers ──────────────────────────────────────────────────────

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


async def _intercept_api(url: str, timeout: int = 30000) -> tuple[str, list[dict]]:
    """
    Load `url` in Playwright, intercept all JSON API responses,
    return (page_html, [list of parsed JSON responses]).
    """
    from playwright.async_api import async_playwright

    api_responses: list[dict] = []
    html = ""

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

        async def handle_response(response):
            ct = response.headers.get("content-type", "")
            if "json" in ct and response.status == 200:
                try:
                    body = await response.json()
                    api_responses.append({"url": response.url, "data": body})
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout)
            await asyncio.sleep(2)
            html = await page.content()
        finally:
            await browser.close()

    return html, api_responses


async def _fetch_json(url: str) -> Any:
    """Direct HTTP JSON fetch (no proxy)."""
    import aiohttp
    import ssl

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE_URL + "/",
    }
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        async with session.get(url) as resp:
            return await resp.json(content_type=None)


# ─── City list ───────────────────────────────────────────────────────────────

async def get_cities() -> list[dict]:
    """
    Returns list of {name, slug, url} by loading the main page and
    either parsing API responses or scraping navigation links.
    """
    html, api_responses = await _intercept_api(BASE_URL)

    # Try to find a cities list in API responses
    for resp in api_responses:
        data = resp["data"]
        cities = _extract_cities_from_json(data)
        if cities:
            return cities

    # Fallback: parse HTML navigation
    return _extract_cities_from_html(html)


def _extract_cities_from_json(data: Any) -> list[dict]:
    cities = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("city")
                slug = item.get("slug") or item.get("code") or item.get("id")
                if name and slug:
                    cities.append({"name": str(name), "slug": str(slug), "url": f"{BASE_URL}/{slug}/"})
    elif isinstance(data, dict):
        for key in ("cities", "regions", "items", "data", "results"):
            if key in data and isinstance(data[key], list):
                cities = _extract_cities_from_json(data[key])
                if cities:
                    return cities
    return cities


def _extract_cities_from_html(html: str) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    cities = []
    seen = set()

    # Look for links matching /<slug>/ pattern (single path segment)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if re.match(r"^/[a-z][a-z0-9\-]{2,}/?$", href) and text and len(text) > 2:
            slug = href.strip("/")
            if slug in ("about", "api", "map", "help", "faq", "login", "register"):
                continue
            if slug not in seen:
                seen.add(slug)
                cities.append({"name": text, "slug": slug, "url": BASE_URL + href})

    return cities


# ─── City fuel info ──────────────────────────────────────────────────────────

async def get_fuel_info(city_slug: str) -> CityFuelInfo:
    """Scrape fuel availability and prices for a given city slug."""
    city_url = f"{BASE_URL}/{city_slug}/"
    try:
        html, api_responses = await _intercept_api(city_url)
    except Exception as e:
        return CityFuelInfo(city=city_slug, city_slug=city_slug, error=str(e))

    # Try parsing stations from intercepted API responses
    for resp in api_responses:
        stations = _extract_stations_from_json(resp["data"])
        if stations:
            city_name = _extract_city_name(html, city_slug)
            return CityFuelInfo(
                city=city_name,
                city_slug=city_slug,
                stations=stations,
                total=len(stations),
            )

    # Try to find city-specific API endpoints from page source
    api_urls = _find_api_urls(html, city_slug)
    for api_url in api_urls:
        try:
            data = await _fetch_json(api_url)
            stations = _extract_stations_from_json(data)
            if stations:
                city_name = _extract_city_name(html, city_slug)
                return CityFuelInfo(
                    city=city_name,
                    city_slug=city_slug,
                    stations=stations,
                    total=len(stations),
                )
        except Exception:
            continue

    # Fallback: parse HTML directly
    city_name = _extract_city_name(html, city_slug)
    stations = _extract_stations_from_html(html)
    return CityFuelInfo(
        city=city_name,
        city_slug=city_slug,
        stations=stations,
        total=len(stations),
    )


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


def _find_api_urls(html: str, city_slug: str) -> list[str]:
    """Extract potential API URLs from page JS source."""
    urls = []
    # Look for fetch/axios/XHR calls in script tags
    for m in re.finditer(r'["\'](/api/[^"\']+|https?://[^"\']*api[^"\']*)["\']', html):
        url = m.group(1)
        if not url.startswith("http"):
            url = BASE_URL + url
        if city_slug in url or "station" in url or "azs" in url or "fuel" in url:
            urls.append(url)
    # Common patterns
    urls += [
        f"{BASE_URL}/api/stations?city={city_slug}",
        f"{BASE_URL}/api/azs?city={city_slug}",
        f"{BASE_URL}/api/v1/stations/{city_slug}",
        f"{BASE_URL}/api/v1/cities/{city_slug}/stations",
    ]
    return list(dict.fromkeys(urls))  # deduplicate preserving order


def _extract_stations_from_json(data: Any) -> list[FuelStation]:
    stations: list[FuelStation] = []

    if isinstance(data, list):
        for item in data:
            s = _parse_station_json(item)
            if s:
                stations.append(s)
    elif isinstance(data, dict):
        for key in ("stations", "azs", "items", "data", "results", "features"):
            if key in data:
                val = data[key]
                if isinstance(val, list):
                    for item in val:
                        s = _parse_station_json(item)
                        if s:
                            stations.append(s)
                    if stations:
                        return stations
        # GeoJSON FeatureCollection
        if data.get("type") == "FeatureCollection":
            for feature in data.get("features", []):
                s = _parse_station_geojson(feature)
                if s:
                    stations.append(s)

    return stations


def _parse_station_json(item: Any) -> Optional[FuelStation]:
    if not isinstance(item, dict):
        return None

    name = (
        item.get("name") or item.get("title") or
        item.get("brand") or item.get("network") or ""
    )
    address = item.get("address") or item.get("addr") or item.get("street") or ""
    lat = float(item.get("lat") or item.get("latitude") or 0)
    lon = float(item.get("lon") or item.get("lng") or item.get("longitude") or 0)
    updated = str(item.get("updated_at") or item.get("updated") or item.get("time") or "")

    fuel_types: dict[str, str] = {}

    # Nested fuels list
    for fuel_key in ("fuels", "fuel", "prices"):
        if fuel_key in item and isinstance(item[fuel_key], list):
            for f in item[fuel_key]:
                if isinstance(f, dict):
                    ft = f.get("type") or f.get("name") or f.get("fuel_type") or ""
                    label = FUEL_LABELS.get(ft.lower(), ft)
                    status_raw = f.get("status") or f.get("available")
                    price = f.get("price") or f.get("cost") or ""
                    status_str = FUEL_STATUS_LABELS.get(status_raw, "")
                    if price:
                        fuel_types[label] = f"{status_str} {price} ₽/л".strip()
                    elif status_str:
                        fuel_types[label] = status_str
        elif fuel_key in item and isinstance(item[fuel_key], dict):
            for ft, val in item[fuel_key].items():
                label = FUEL_LABELS.get(ft.lower(), ft)
                if isinstance(val, dict):
                    status_raw = val.get("status") or val.get("available")
                    price = val.get("price") or ""
                    status_str = FUEL_STATUS_LABELS.get(status_raw, "")
                    if price:
                        fuel_types[label] = f"{status_str} {price} ₽/л".strip()
                    elif status_str:
                        fuel_types[label] = status_str
                elif val:
                    fuel_types[label] = str(val)

    # Inline fuel status fields like ai92_status, price_ai95, etc.
    for k, v in item.items():
        m = re.match(r"(ai\d+|dt|diesel|gas|petrol\d*)_?(status|price|available)?", k.lower())
        if m and v is not None:
            ft = m.group(1)
            label = FUEL_LABELS.get(ft, ft.upper())
            suffix = m.group(2) or ""
            if suffix == "price" and v:
                fuel_types.setdefault(label, f"{v} ₽/л")
            elif suffix in ("status", "available") and v:
                s = FUEL_STATUS_LABELS.get(v, str(v))
                if label in fuel_types:
                    fuel_types[label] = f"{s} / {fuel_types[label]}"
                else:
                    fuel_types[label] = s

    if not name and not address and not fuel_types:
        return None

    return FuelStation(
        name=name,
        address=address,
        lat=lat,
        lon=lon,
        fuel_types=fuel_types,
        updated=updated,
    )


def _parse_station_geojson(feature: dict) -> Optional[FuelStation]:
    props = feature.get("properties", {})
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [0, 0])
    item = {**props, "lon": coords[0] if coords else 0, "lat": coords[1] if len(coords) > 1 else 0}
    return _parse_station_json(item)


def _extract_stations_from_html(html: str) -> list[FuelStation]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    stations: list[FuelStation] = []

    # Table rows
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            s = FuelStation(
                name=cells[0] if cells else "",
                address=cells[1] if len(cells) > 1 else "",
            )
            for i, h in enumerate(headers[2:], start=2):
                if i < len(cells) and cells[i]:
                    s.fuel_types[h] = cells[i]
            if s.name:
                stations.append(s)

    # Card / block elements
    if not stations:
        for card in soup.find_all(class_=re.compile(r"(station|card|azs|item)", re.I)):
            name_el = card.find(class_=re.compile(r"(name|title)", re.I)) or card.find(["h2", "h3", "h4"])
            addr_el = card.find(class_=re.compile(r"(addr|address)", re.I))
            name = name_el.get_text(strip=True) if name_el else ""
            addr = addr_el.get_text(strip=True) if addr_el else ""
            if not name:
                continue
            s = FuelStation(name=name, address=addr)
            for price_el in card.find_all(class_=re.compile(r"(price|fuel|cost)", re.I)):
                text = price_el.get_text(strip=True)
                m = re.search(r"(АИ[-\s]?\d+|ДТ|Газ).*?(\d+[\.,]\d+)", text, re.I)
                if m:
                    s.fuel_types[m.group(1)] = f"{m.group(2)} ₽/л"
            stations.append(s)

    return stations


# ─── City search ─────────────────────────────────────────────────────────────

async def search_city(query: str) -> list[dict]:
    """Search for cities matching the query string."""
    cities = await get_cities()
    q = query.lower().strip()
    return [c for c in cities if q in c["name"].lower() or q in c["slug"].lower()]
