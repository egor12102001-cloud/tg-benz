"""
Scraper for gdebenz.ru API.

API endpoint:
  GET https://gdebenz.ru/api/nearby?lat=LAT&lon=LON&radius_km=RADIUS

Returns JSON with stations list and summary.
No auth, no Playwright needed — pure HTTP.
"""
import aiohttp
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional

API_URL = "https://gdebenz.ru/api/nearby"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

CACHE_TTL_SECONDS = 180  # 3 минуты

STATUS_EMOJI = {
    "yes":   "✅",
    "queue": "🟡",
    "low":   "🟠",
    "no":    "❌",
}
STATUS_LABEL = {
    "yes":   "Есть",
    "queue": "Очередь",
    "low":   "Мало",
    "no":    "Нет",
}
STATUS_ORDER = {"yes": 0, "queue": 1, "low": 2, "no": 3, None: 4}


@dataclass
class Station:
    name: str
    brand: str
    addr: str
    lat: float
    lon: float
    distance_km: float
    status: Optional[str]       # "yes" | "queue" | "low" | "no" | None
    detail: str                 # e.g. "92, 95, ДТ · Большая очередь"
    fuels_now: str              # e.g. "92,95,ДТ"
    confirmations: int
    confirmed: bool
    last_at: str


@dataclass
class NearbyResult:
    city: str
    lat: float
    lon: float
    radius_km: int
    summary: dict[str, int]     # {"yes": N, "queue": N, "low": N, "no": N}
    stations: list[Station]
    updated: str
    error: Optional[str] = None


def normalize_city(city_name: str) -> str:
    """
    Normalize a raw user input into a comparable city key:
    strips whitespace, leading slash-commands, lowercases.
    """
    text = city_name.strip()
    # Drop accidental leading command text like ": /fuel Александров"
    text = re.sub(r"^[:\s]*\/\w+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


# In-memory cache: normalized_city -> (timestamp, NearbyResult)
_cache: dict[str, tuple[float, "NearbyResult"]] = {}


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://gdebenz.ru/",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }


async def geocode_city(city_name: str) -> Optional[tuple[float, float, str]]:
    """
    Resolve city name → (lat, lon, display_name) using Nominatim.
    Returns None if not found.
    """
    params = {
        "q": city_name,
        "countrycodes": "ru",
        "format": "json",
        "limit": 3,
        "addressdetails": 0,
    }
    nom_headers = {
        "User-Agent": "tg-benz-bot/1.0 (fuel availability bot)",
        "Accept-Language": "ru",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                NOMINATIM_URL, params=params, headers=nom_headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        return None

    if not data:
        return None

    # Prefer cities/towns over other place types
    for item in data:
        if item.get("type") in ("city", "town", "village", "municipality"):
            return float(item["lat"]), float(item["lon"]), item.get("display_name", city_name)

    item = data[0]
    return float(item["lat"]), float(item["lon"]), item.get("display_name", city_name)


async def reverse_geocode(lat: float, lon: float) -> Optional[tuple[float, float, str]]:
    """
    Resolve coordinates → (lat, lon, city_display_name) using Nominatim reverse.
    Returns None if not found.
    """
    params = {
        "lat": lat, "lon": lon,
        "format": "json",
        "zoom": 10,
        "addressdetails": 1,
    }
    nom_headers = {
        "User-Agent": "tg-benz-bot/1.0 (fuel availability bot)",
        "Accept-Language": "ru",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                NOMINATIM_REVERSE_URL, params=params, headers=nom_headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception:
        return None

    if not data or "address" not in data:
        return None

    addr = data["address"]
    city = (
        addr.get("city") or addr.get("town") or addr.get("village")
        or addr.get("municipality") or addr.get("county") or data.get("display_name", "")
    )
    return lat, lon, city


async def get_nearby_stations(
    lat: float, lon: float, radius_km: int = 20
) -> tuple[dict, list[dict]]:
    """
    Call gdebenz.ru/api/nearby and return (summary, stations_list).
    """
    params = {"lat": round(lat, 4), "lon": round(lon, 4), "radius_km": radius_km}
    connector = aiohttp.TCPConnector(ssl=_ssl_ctx())

    async with aiohttp.ClientSession(connector=connector, headers=_headers()) as session:
        async with session.get(
            API_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    summary = data.get("summary", {})
    raw_stations = data.get("stations", [])
    return summary, raw_stations


def parse_stations(raw: list[dict]) -> list[Station]:
    stations = []
    for s in raw:
        stations.append(Station(
            name=s.get("name") or s.get("brand") or "АЗС",
            brand=s.get("brand") or "",
            addr=s.get("addr") or "",
            lat=float(s.get("lat") or 0),
            lon=float(s.get("lon") or 0),
            distance_km=float(s.get("distance_km") or 0),
            status=s.get("status"),
            detail=s.get("detail") or "",
            fuels_now=s.get("fuels_now") or "",
            confirmations=int(s.get("confirmations") or 0),
            confirmed=bool(s.get("confirmed")),
            last_at=s.get("last_at") or "",
        ))
    # Sort: yes → queue → low → no → unknown, then by distance
    stations.sort(key=lambda s: (STATUS_ORDER.get(s.status, 4), s.distance_km))
    return stations


async def fetch_city_fuel(city_name: str, radius_km: int = 20, use_cache: bool = True) -> NearbyResult:
    """Main entry point: city name → NearbyResult with stations. Cached for CACHE_TTL_SECONDS."""

    cache_key = f"{normalize_city(city_name)}:{radius_km}"
    if use_cache:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    # 1. Geocode
    geo = await geocode_city(city_name)
    if geo is None:
        return NearbyResult(
            city=city_name, lat=0, lon=0, radius_km=radius_km,
            summary={}, stations=[], updated="",
            error=f"Город «{city_name}» не найден. Попробуйте указать точнее.",
        )
    lat, lon, display_name = geo
    # Use just the first part of Nominatim display_name (city name in Russian)
    short_name = display_name.split(",")[0].strip()

    # 2. Fetch stations
    try:
        summary, raw = await get_nearby_stations(lat, lon, radius_km)
    except Exception as e:
        return NearbyResult(
            city=short_name, lat=lat, lon=lon, radius_km=radius_km,
            summary={}, stations=[], updated="",
            error=f"Ошибка при получении данных: {e}",
        )

    stations = parse_stations(raw)
    result = NearbyResult(
        city=short_name, lat=lat, lon=lon, radius_km=radius_km,
        summary=summary, stations=stations, updated="",
    )
    _cache[cache_key] = (time.monotonic(), result)
    return result
