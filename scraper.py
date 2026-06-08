#!/usr/bin/env python3
"""
Vakantiewoning Scraper - v6
============================
Bronnen  : recreatievastgoed.nl · Marktplaats (per provincie) ·
           vakantiehuistekoop.nl · Landal Makelaardij ·
           RecreatiewoningenTekoop.nl · EuroParcs Makelaardij ·
           Veluwechalets.nl · UwTweedeHuisMakelaar.nl ·
           UwBuitenleven.nl · TopParkenVerkoop.nl ·
           Vakantiemakelaar.nl · CenterParcs Vastgoed ·
           Jaap.nl · Huislijn.nl
Notificatie : HTML-e-mail via SMTP (nieuw / 14-daagse alert)
Website  : docs/data.json → GitHub Pages (docs/index.html)
"""

import json
import logging
import os
import re
import smtplib
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Supabase (optioneel — alleen actief als SUPABASE_URL + SUPABASE_KEY geconfigureerd zijn)
try:
    from supabase import create_client as _supabase_create_client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False

import config

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# ── Thread-local sessie (thread-safe voor parallelle scrapers) ─
_thread_local = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.session = s
    return _thread_local.session

# Behoud globale SESSION voor backward compat (huislijn scraper gebruikt hem direct)
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

WEBSITE_URL        = "https://mcv13-wp.github.io/vacay-cabin"
NOMINATIM_URL      = "https://nominatim.openstreetmap.org/search"
SCREENSHOTS_DIR    = Path("docs/screenshots")
GEOCODE_CACHE_FILE = Path("geocode_cache.json")
ALERT_DAYS         = 14
OFFLINE_DAYS       = 90   # listings ouder dan N dagen offline worden verwijderd


# ════════════════════════════════════════════════════════════
#  Persistentie
# ════════════════════════════════════════════════════════════

def load_known() -> tuple[dict[str, dict], dict]:
    path = Path(config.KNOWN_LISTINGS_FILE)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        meta = raw.pop("_meta", {})
        return raw, meta
    return {}, {}


def save_known(listings: dict[str, dict], meta: dict) -> None:
    data = dict(listings)
    data["_meta"] = meta
    with open(config.KNOWN_LISTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("known_listings.json: %d woningen opgeslagen", len(listings))


def url_key(url: str) -> str:
    url = url.strip().split("#")[0].split("?")[0].rstrip("/")
    return url.lower()


def write_data_json(
    all_listings: list[dict],
    new_listings: list[dict],
    run_stats: dict | None = None,
) -> None:
    docs_dir = Path(config.DATA_JSON_FILE).parent
    docs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "new_listings": new_listings,
        "all_listings": all_listings,
    }
    if run_stats:
        payload["run_stats"] = run_stats
    with open(config.DATA_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("docs/data.json bijgewerkt (%d nieuw, %d totaal)",
             len(new_listings), len(all_listings))


# ════════════════════════════════════════════════════════════
#  Hulpfuncties
# ════════════════════════════════════════════════════════════

def extract_price(text: str) -> int | None:
    """
    Parst Nederlandse prijsnotaties:
      "€ 285.000"   → 285000   (punten als duizendscheidingsteken)
      "€ 285.000,-" → 285000   (met afsluit-streep)
      "€ 285k"      → 285000   (k als afkorting voor kilo/duizend)
      "285K"        → 285000   (ook zonder €-teken, hoofdletter)
      "€ 285,5k"    → 285500   (met decimaal)
    """
    # Patroon 1: k/K-suffix — "€ 285k", "285K", "€ 285,5k"
    # \b zorgt dat "10km" of "5kg" niet matcht
    m = re.search(r"[€]?\s*(\d+(?:[.,]\d+)?)\s*[kK](?=\b|\s|$|\.)", text)
    if m:
        val = m.group(1).replace(",", ".")
        try:
            return round(float(val) * 1000)
        except ValueError:
            pass

    # Patroon 2: standaard notatie "€ 285.000" of "€ 285.000,-"
    m = re.search(r"[€]\s*([\d\.]+)(?:,-)?", text)
    if m:
        return int(re.sub(r"\D", "", m.group(1)))

    return None


# ── Retry helper voor get_page (tenacity: 3 pogingen, 2-10s backoff) ──

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,   # gooit na 3 mislukte pogingen alsnog de fout
)
def _fetch_url(url: str, timeout: int) -> requests.Response:
    """Interne fetch met automatische retry. Gebruik get_page() in scraper-code."""
    resp = _get_session().get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def get_page(url: str, timeout: int = 15) -> BeautifulSoup | None:
    """Haalt een pagina op met exponential-backoff retry (max 3 pogingen)."""
    try:
        resp = _fetch_url(url, timeout)
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("Fout bij ophalen %s (na 3 pogingen): %s", url, exc)
        return None


def is_sold(text: str) -> bool:
    return any(kw in text.lower() for kw in config.SOLD_KEYWORDS)


def is_in_region(text: str) -> bool:
    r"""
    Controleert of tekst een regio-trefwoord bevat.
    Gebruikt \b word-boundaries zodat bijv. 'ede' niet matcht in 'leiden'.
    Voor keywords die beginnen/eindigen met niet-woordtekens (apostrof, koppelteken)
    wordt een (?<!\w)/(?!\w) lookaround gebruikt.
    """
    text_lower = text.lower()
    for kw in config.REGION_KEYWORDS:
        # Kies de juiste boundary op basis van het eerste/laatste teken
        prefix = r"\b" if kw[0].isalnum()  else r"(?<!\w)"
        suffix = r"\b" if kw[-1].isalnum() else r"(?!\w)"
        if re.search(prefix + re.escape(kw) + suffix, text_lower):
            return True
    return False


def passes_filters(l: dict) -> bool:
    if l.get("sold"):
        return False
    if l.get("price") is not None and l["price"] > config.MAX_PRICE:
        return False
    if l.get("bedrooms") is not None and l["bedrooms"] < config.MIN_BEDROOMS:
        return False
    if l.get("persons") is not None and l["persons"] < config.MIN_PERSONS:
        return False
    return True
def _dedup_cross_source(listings: list[dict]) -> list[dict]:
    """Verwijder near-duplicaten op basis van prijs + locatie (cross-source)."""
    import difflib
    kept = []
    for l in listings:
        is_dup = False
        for k in kept:
            if k.get("source") == l.get("source"):
                continue
            price_match = (
                k.get("price") and l.get("price") and
                abs(k["price"] - l["price"]) < 2000
            )
            loc_sim = difflib.SequenceMatcher(
                None,
                (k.get("location") or "").lower(),
                (l.get("location") or "").lower()
            ).ratio()
            if price_match and loc_sim > 0.7:
                is_dup = True
                log.debug("Cross-source dup: %s (%s) ≈ %s (%s)",
                          l.get("title","")[:30], l.get("source",""),
                          k.get("title","")[:30], k.get("source",""))
                break
        if not is_dup:
            kept.append(l)
    return kept

def _safe_int(val: str) -> int | None:
    m = re.search(r"(\d+)", str(val).strip())
    return int(m.group(1)) if m else None


def is_complete(l: dict) -> bool:
    """Vereist: geldige URL + prijs + locatie/titel."""
    url = (l.get("url") or "").strip()
    if not url.startswith("http"):
        return False
    if l.get("price") is None:
        return False
    location_text = " ".join(
        filter(None, [l.get("location"), l.get("title")])
    ).strip()
    if not location_text:
        return False
    return True


# ════════════════════════════════════════════════════════════
#  Geocoding cache  (geocode_cache.json)
# ════════════════════════════════════════════════════════════

# Module-level cache dict: genormaliseerde locatie → (lat, lng) of None
_geocode_cache: dict[str, tuple[float, float] | None] = {}


def _load_geocode_cache() -> None:
    """Laad persistente geocoding-cache van schijf (als het bestand bestaat)."""
    global _geocode_cache
    if GEOCODE_CACHE_FILE.exists():
        try:
            with open(GEOCODE_CACHE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            _geocode_cache = {
                k: (tuple(v) if v else None)   # type: ignore[misc]
                for k, v in raw.items()
            }
            log.info("Geocoding-cache geladen: %d locaties", len(_geocode_cache))
        except Exception as exc:
            log.warning("Geocoding-cache laden mislukt: %s", exc)
            _geocode_cache = {}


def _save_geocode_cache() -> None:
    """Schrijf de geocoding-cache terug naar schijf."""
    try:
        with open(GEOCODE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {k: list(v) if v else None for k, v in _geocode_cache.items()},
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:
        log.warning("Geocoding-cache opslaan mislukt: %s", exc)


def _normalize_location(location: str) -> str:
    """Normaliseer locatiestring voor gebruik als cache-sleutel."""
    s = re.sub(r"\|\s*nederland", "", location, flags=re.IGNORECASE)
    return s.strip().rstrip(",").strip().lower()


# ════════════════════════════════════════════════════════════
#  Supabase integratie
# ════════════════════════════════════════════════════════════

def _get_supabase_client():
    """Geeft een Supabase-client (secret key) terug, of None als niet geconfigureerd."""
    if not _SUPABASE_AVAILABLE:
        return None
    url = getattr(config, "SUPABASE_URL", "") or os.environ.get("SUPABASE_URL", "")
    key = getattr(config, "SUPABASE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        return None
    try:
        return _supabase_create_client(url, key)
    except Exception as exc:
        log.warning("Supabase client aanmaken mislukt: %s", exc)
        return None


def _load_blocked_urls() -> set[str]:
    """
    Laad geblokkeerde url_keys uit Supabase tabel 'blocked_urls'.
    Geeft lege set terug bij elke fout zodat de scraper altijd doorgaat.
    """
    client = _get_supabase_client()
    if not client:
        log.debug("Supabase niet geconfigureerd — blocked_urls overgeslagen")
        return set()
    try:
        resp   = client.table("blocked_urls").select("url_key").execute()
        result = {row["url_key"] for row in (resp.data or [])}
        log.info("Supabase: %d geblokkeerde URL-keys geladen", len(result))
        return result
    except Exception as exc:
        log.warning("Supabase blocked_urls laden mislukt (scraper gaat door): %s", exc)
        return set()


def _upsert_to_supabase(
    listings: dict[str, dict],
    new_url_keys: set[str],
    blocked_url_keys: set[str],
) -> None:
    """
    Upsert alle woningen naar Supabase tabel 'listings'.
    Velden: url_key, source, title, url, price, bedrooms, persons, location,
            image, sold, offline, deleted, is_new, first_seen,
            price_history (JSON string), lat, lng, updated_at.
    Bij elke fout wordt alleen een waarschuwing gelogd — data.json blijft fallback.
    """
    client = _get_supabase_client()
    if not client:
        log.debug("Supabase niet geconfigureerd — upsert overgeslagen")
        return
    try:
        now  = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "url_key":       uk,
                "source":        l.get("source"),
                "title":         l.get("title"),
                "url":           l.get("url"),
                "price":         l.get("price"),
                "bedrooms":      l.get("bedrooms"),
                "persons":       l.get("persons"),
                "location":      l.get("location"),
                "image":         l.get("image"),
                "sold":          bool(l.get("sold")),
                "offline":       bool(l.get("offline")),
                # Geblokkeerde listings worden als deleted gemarkeerd
                "deleted":       uk in blocked_url_keys or bool(l.get("deleted")),
                "is_new":        uk in new_url_keys,
                "first_seen":    l.get("first_seen"),
                "price_history": json.dumps(
                    l.get("price_history", []), ensure_ascii=False
                ),
                "lat":           l.get("lat"),
                "lng":           l.get("lng"),
                "updated_at":    now,
            }
            for uk, l in listings.items()
        ]
        # Upsert in batches van 100 (Supabase row limit per request)
        batch = 100
        for i in range(0, len(rows), batch):
            client.table("listings").upsert(rows[i : i + batch]).execute()
        log.info("Supabase: %d woningen ge-upsert", len(rows))
    except Exception as exc:
        log.warning("Supabase upsert mislukt (data.json blijft fallback): %s", exc)


def geocode(location: str) -> tuple[float, float] | None:
    """
    Converteert een locatiestring naar (lat, lng) via Nominatim.
    Resultaten worden gecached in geocode_cache.json — bestaande locaties
    worden nooit opnieuw bij Nominatim opgehaald.
    Strips '| Nederland' automatisch; fallback naar alleen stadsnaam.
    """
    cache_key = _normalize_location(location)

    # Cache-hit: direct retourneren (ook None-resultaten worden gecached)
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]   # type: ignore[return-value]

    hdrs  = {"User-Agent": "vacay-cabin-scraper/1.0 (mcversteeg@outlook.com)"}
    clean = re.sub(r"\|\s*nederland", "", location, flags=re.IGNORECASE)
    clean = clean.strip().rstrip(",").strip()

    result: tuple[float, float] | None = None
    for query in [clean, clean.split(",")[0].strip()]:
        if not query:
            continue
        try:
            params = {
                "q": f"{query}, Nederland",
                "format": "json",
                "limit": 1,
                "countrycodes": "nl",
            }
            resp = requests.get(NOMINATIM_URL, params=params, headers=hdrs, timeout=10)
            data = resp.json()
            if data:
                result = (float(data[0]["lat"]), float(data[0]["lon"]))
                break
        except Exception as exc:
            log.debug("Geocoding mislukt voor '%s': %s", query, exc)
        time.sleep(1.1)

    # Sla op in cache (ook None, zodat mislukte lookups niet herhaald worden)
    _geocode_cache[cache_key] = result
    _save_geocode_cache()
    return result


def take_screenshot(url: str, save_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url, timeout=25_000, wait_until="domcontentloaded")
            page.screenshot(
                path=str(save_path),
                clip={"x": 0, "y": 0, "width": 1280, "height": 720},
            )
            browser.close()
        log.info("Screenshot opgeslagen: %s", save_path.name)
        return True
    except ImportError:
        log.debug("Playwright niet geïnstalleerd – screenshot overgeslagen")
        return False
    except Exception as exc:
        log.debug("Screenshot mislukt voor %s: %s", url, exc)
        return False


def _parse_persons_range(val: str) -> int | None:
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", val.strip())
    if m:
        return int(m.group(2))
    m2 = re.search(r"(\d+)", val.strip())
    return int(m2.group(1)) if m2 else None


# ════════════════════════════════════════════════════════════
#  Scraper: recreatievastgoed.nl
# ════════════════════════════════════════════════════════════

def scrape_recreatievastgoed() -> list[dict]:
    log.info("Scrapen: recreatievastgoed.nl")
    listings: list[dict] = []
    base = "https://recreatievastgoed.nl"

    soup = get_page(f"{base}/aanbod/")
    if not soup:
        log.warning("recreatievastgoed.nl: niet bereikbaar")
        return listings

    page_numbers = [int(m) for m in re.findall(r"/aanbod/page/(\d+)/", soup.decode())]
    max_page = max(page_numbers, default=1)
    log.info("recreatievastgoed.nl: %d pagina's", max_page)

    for page in range(1, max_page + 1):
        url = f"{base}/aanbod/" if page == 1 else f"{base}/aanbod/page/{page}/"
        soup = get_page(url)
        if not soup:
            break
        for card in soup.select(".c-property-results-item"):
            listing = _parse_rv_card(card, base)
            if listing:
                listings.append(listing)
        if page < max_page:
            time.sleep(0.8)

    log.info("recreatievastgoed.nl: %d woningen", len(listings))
    return listings


def _parse_rv_card(card, base: str) -> dict | None:
    link_tag = card.select_one(".c-property-results-item__link")
    if not link_tag:
        return None
    raw_url = link_tag.get("href", "")
    full_url = raw_url if raw_url.startswith("http") else base + raw_url

    city    = card.select_one(".u-text-city")
    prov    = card.select_one(".u-text-province_country")
    price_e = card.select_one(".u-text-price")
    img_el  = card.select_one("img")

    city_t   = city.get_text(strip=True) if city else ""
    prov_t   = prov.get_text(strip=True) if prov else ""
    location = f"{city_t}, {prov_t}".strip(", ")
    price    = extract_price(price_e.get_text(strip=True) if price_e else "")
    image    = img_el.get("src", "") if img_el else ""

    persons = None
    for detail in card.select(".c-detail"):
        svg = detail.find("svg")
        if svg and "icon-persons" in " ".join(svg.get("class", [])):
            lbl = detail.select_one(".c-detail__label")
            if lbl:
                persons = _safe_int(lbl.get_text())

    full_text = card.get_text(" ", strip=True)
    return {
        "source":   "Recreatievastgoed.nl",
        "title":    f"{city_t} – {prov_t}".strip(" –"),
        "url":      full_url,
        "price":    price,
        "bedrooms": None,
        "persons":  persons,
        "location": location,
        "image":    image,
        "sold":     is_sold(full_text),
        "raw":      full_text[:200],
    }


def enrich_with_details(listings: list[dict]) -> list[dict]:
    """
    Haal slaapkamers, personen en foto op van de detailpagina voor listings
    waarbij deze info ontbreekt.  Geocoding wordt NIET hier gedaan (dat
    loopt apart via stap 3c in run() om altijd uitgevoerd te worden).
    """
    for listing in listings:
        if listing.get("bedrooms") is not None:
            continue  # al volledig, sla over
        url = listing.get("url", "")
        if not url.startswith("http"):
            continue

        soup = get_page(url)
        if not soup:
            continue
        text = soup.get_text(" ")

        m = re.search(r"Aantal\s+slaapkamers\s+(\d+)", text, re.IGNORECASE)
        if not m:
            m = re.search(r"(\d+)\s+slaapkamer", text, re.IGNORECASE)
        if m:
            listing["bedrooms"] = int(m.group(1))

        if listing.get("persons") is None:
            mp = (re.search(r"Aantal\s+personen\s+(\d+)", text, re.IGNORECASE)
                  or re.search(r"(\d+)[- ]persoons", text, re.IGNORECASE))
            if mp:
                listing["persons"] = int(mp.group(1))

        if not listing.get("image"):
            og = soup.find("meta", property="og:image")
            if og:
                listing["image"] = og.get("content", "")

        time.sleep(0.5)

    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Marktplaats — per provincie, vereist beds + pers
# ════════════════════════════════════════════════════════════

# RegionIds per provincie (uit Marktplaats URL-structuur)
_MP_PROVINCES = {
    "gelderland":   4555,
    "overijssel":   4557,
    "drenthe":      4561,
    "limburg":      4556,
    "noord-brabant": 4554,
    "utrecht":      4558,
}


def scrape_marktplaats() -> list[dict]:
    log.info("Scrapen: Marktplaats recreatiewoningen (per provincie)")
    listings: list[dict] = []
    seen_ids: set[str] = set()

    for province, region_id in _MP_PROVINCES.items():
        page = 0
        while True:
            url = (
                f"https://www.marktplaats.nl/l/huizen-en-kamers/"
                f"recreatiewoningen-te-koop/f/{province}/{region_id}/"
                f"?PriceTo={config.MAX_PRICE}&currentPage={page}"
            )
            soup = get_page(url)
            if not soup:
                break
            script = soup.find("script", string=re.compile(r"itemId|vipUrl"))
            if not script or not script.string:
                break
            try:
                data = json.loads(script.string)
            except json.JSONDecodeError:
                break

            page_items = _find_mp_listings(data)
            if not page_items:
                break

            new_on_page = 0
            for item in page_items:
                item_id = item.get("itemId", "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                new_on_page += 1

                price_cents = item.get("priceInfo", {}).get("priceCents") or 0
                price  = int(price_cents / 100) if price_cents else None
                city   = item.get("location", {}).get("cityName", "").strip()
                desc   = item.get("description") or ""
                title  = item.get("title", "").strip()
                vip    = item.get("vipUrl", "")
                url_l  = f"https://www.marktplaats.nl{vip}" if vip.startswith("/") else vip
                imgs   = item.get("imageUrls") or []
                image  = imgs[0] if imgs else ""

                # ── Failsafe: vereist URL, prijs, stad ──────────
                if not url_l.startswith("http") or price is None or not city:
                    log.debug("Marktplaats: overgeslagen (onvolledig) – %s", title)
                    continue

                bed_m  = re.search(r"(\d+)\s*slaapkamer", desc, re.IGNORECASE)
                pers_m = re.search(
                    r"(\d+)[- ]persoons|(\d+)\s*personen", desc, re.IGNORECASE
                )
                beds   = int(bed_m.group(1)) if bed_m else None
                persons = int(pers_m.group(1) or pers_m.group(2)) if pers_m else None

                # ── Stricter: vereist slaapkamer- én personeninformatie ──
                if beds is None or persons is None:
                    log.debug(
                        "Marktplaats: overgeslagen (geen beds/pers) – %s", title
                    )
                    continue

                listings.append({
                    "source":   "Marktplaats",
                    "title":    title,
                    "url":      url_l,
                    "price":    price,
                    "bedrooms": beds,
                    "persons":  persons,
                    "location": f"{city}, {province.replace('-', ' ').title()}",
                    "image":    image,
                    "sold":     bool(item.get("reserved", False)),
                    "raw":      f"{title} {city} {desc[:150]}",
                })

            if new_on_page == 0:
                break
            page += 1
            time.sleep(0.8)

    log.info("Marktplaats: %d woningen (per provincie, met beds+pers)", len(listings))
    return listings


def _find_mp_listings(obj, depth: int = 0) -> list | None:
    if depth > 10:
        return None
    if isinstance(obj, list) and len(obj) > 1:
        if isinstance(obj[0], dict) and ("itemId" in obj[0] or "vipUrl" in obj[0]):
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _find_mp_listings(v, depth + 1)
            if r:
                return r
    return None


# ════════════════════════════════════════════════════════════
#  Scraper: vakantiehuistekoop.nl
# ════════════════════════════════════════════════════════════

def scrape_vakantiehuistekoop() -> list[dict]:
    log.info("Scrapen: vakantiehuistekoop.nl")
    base = "https://www.vakantiehuistekoop.nl"
    soup = get_page(f"{base}/")
    if not soup:
        return []

    listings: list[dict] = []
    cards = [c for c in soup.select("[class*=property-card]") if "€" in c.get_text()]
    for card in cards:
        title_el = card.select_one(".property-card__title")
        price_el = card.select_one(".property-card__price")
        link_el  = card.select_one("a[href]")
        img_el   = card.select_one("img")

        title = title_el.get_text(" ", strip=True) if title_el else "Onbekend"
        price = extract_price(price_el.get_text() if price_el else "")
        href  = link_el["href"] if link_el else ""
        url   = href if href.startswith("http") else base + href
        image = img_el.get("src", "") if img_el else ""

        icon_vals = [v.get_text(strip=True) for v in card.select(".property-card__icon-val")]
        persons  = _parse_persons_range(icon_vals[0] if icon_vals else "")
        bedrooms = _safe_int(icon_vals[1] if len(icon_vals) > 1 else "")

        listings.append({
            "source":   "VakantiehuisTekoop.nl",
            "title":    title,
            "url":      url,
            "price":    price,
            "bedrooms": bedrooms,
            "persons":  persons,
            "location": title,
            "image":    image,
            "sold":     is_sold(card.get_text(" ", strip=True)),
            "raw":      card.get_text(" ", strip=True)[:200],
        })

    log.info("vakantiehuistekoop.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Landal Makelaardij
# ════════════════════════════════════════════════════════════

_LM_TARGET_PROVINCES = {
    "gelderland", "overijssel", "utrecht", "drenthe",
    "noord-brabant", "limburg",
    # Friesland en Zeeland verwijderd (>2,5 uur rijden)
}


def scrape_landalmakelaardij() -> list[dict]:
    log.info("Scrapen: Landal Makelaardij")
    soup_xml = get_page("https://www.landalmakelaardij.nl/vm_object_cpt-sitemap.xml")
    if not soup_xml:
        return []

    all_urls = [loc.get_text().strip() for loc in soup_xml.find_all("loc")]
    target = [
        (url.split("/")[4], url)
        for url in all_urls
        if "/woning/" in url and len(url.split("/")) >= 5
        and url.split("/")[4] in _LM_TARGET_PROVINCES
    ]
    log.info("Landal Makelaardij: %d woningen in doelregio", len(target))

    listings: list[dict] = []
    for province, url in target:
        listing = _parse_lm_detail(url, province)
        if listing:
            listings.append(listing)
        time.sleep(0.4)

    log.info("Landal Makelaardij: %d woningen geparset", len(listings))
    return listings


def _parse_lm_detail(url: str, province: str) -> dict | None:
    soup = get_page(url)
    if not soup:
        return None
    text = soup.get_text(" ", strip=True)

    price = bedrooms = persons = None
    for li in soup.find_all("li"):
        t = li.get_text(" ", strip=True)
        if t.lower().startswith("prijs") and price is None:
            price = extract_price(t)
        if "aantal slaapkamers" in t.lower() and bedrooms is None:
            bedrooms = _safe_int(t)
        if "aantal personen" in t.lower() and persons is None:
            persons = _safe_int(t)

    if persons is None:
        mp = re.search(r"(\d+)[- ]persoons", text, re.IGNORECASE)
        if mp:
            persons = int(mp.group(1))

    h1    = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else url.rstrip("/").split("/")[-1]
    og_img = soup.find("meta", property="og:image")
    image  = og_img.get("content", "") if og_img else ""

    return {
        "source":   "Landal Makelaardij",
        "title":    title,
        "url":      url,
        "price":    price,
        "bedrooms": bedrooms,
        "persons":  persons,
        "location": province.replace("-", " ").title(),
        "image":    image,
        "sold":     is_sold(text),
        "raw":      text[:200],
    }


# ════════════════════════════════════════════════════════════
#  Scraper: RecreatiewoningenTekoop.nl
# ════════════════════════════════════════════════════════════

_RWT_PROVINCES = [
    "gelderland", "overijssel", "drenthe", "limburg", "noord-brabant", "utrecht"
]


def scrape_recreatiewoningentekoop() -> list[dict]:
    log.info("Scrapen: RecreatiewoningenTekoop.nl")
    base      = "https://www.recreatiewoningentekoop.nl"
    listings: list[dict] = []
    seen_urls: set[str]  = set()

    for province in _RWT_PROVINCES:
        page = 1
        while True:
            url  = f"{base}/recreatiewoning/nederland/{province}?pagina={page}"
            soup = get_page(url)
            if not soup:
                break

            # Kaartlinks volgen het patroon /recreatiewoning/nederland/{prov}/{stad}/{slug}
            pattern  = re.compile(
                rf"/recreatiewoning/nederland/{province}/[^/?#]+/[^/?#]+"
            )
            new_on_page = 0

            for a in soup.find_all("a", href=pattern):
                href  = a.get("href", "").split("?")[0]
                url_l = href if href.startswith("http") else base + href
                if url_l in seen_urls:
                    continue
                seen_urls.add(url_l)
                new_on_page += 1

                # Card = dichtstbijzijnde artikel-/list-element
                card      = a.find_parent(["article", "li"]) or a
                full_text = card.get_text(" ", strip=True)
                price     = extract_price(full_text)

                # Titel: h2/h3 in card of linktekst
                title_el = card.find(["h2", "h3", "h4"])
                title    = (
                    title_el.get_text(strip=True) if title_el
                    else a.get_text(strip=True)[:80]
                )
                if not title:
                    parts = href.rstrip("/").split("/")
                    title = parts[-1].replace("-", " ").title()

                img_el = card.find("img")
                image  = img_el.get("src", "") if img_el else ""

                # Locatie uit URL
                parts    = href.rstrip("/").split("/")
                city     = parts[-2].replace("-", " ").title() if len(parts) >= 2 else ""
                location = f"{city}, {province.replace('-', ' ').title()}"

                listings.append({
                    "source":   "RecreatiewoningenTekoop.nl",
                    "title":    title,
                    "url":      url_l,
                    "price":    price,
                    "bedrooms": None,
                    "persons":  None,
                    "location": location,
                    "image":    image,
                    "sold":     is_sold(full_text),
                    "raw":      full_text[:200],
                })

            # Volgende pagina?
            has_next = bool(soup.find("a", href=re.compile(rf"pagina={page + 1}")))
            if not has_next or new_on_page == 0:
                break
            page += 1
            time.sleep(0.8)

    log.info("RecreatiewoningenTekoop.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: EuroParcs Makelaardij
# ════════════════════════════════════════════════════════════

def scrape_europarcsmakelaardij() -> list[dict]:
    log.info("Scrapen: EuroParcs Makelaardij")
    base     = "https://www.europarcsmakelaardij.nl"
    listings: list[dict] = []
    page = 1

    while True:
        url  = f"{base}/woningen/" if page == 1 else f"{base}/woningen/page/{page}/"
        soup = get_page(url)
        if not soup:
            break

        # Geprobeerde selectors op basis van onderzoek
        cards = (
            soup.select(".card-wrapper")
            or soup.select("[class*='card-wrapper']")
            or soup.select("article")
        )
        if not cards:
            break

        new_on_page = 0
        for card in cards:
            title_el = (
                card.select_one(".card-title")
                or card.select_one("h3")
                or card.select_one("h2")
            )
            price_el = (
                card.select_one(".card-price")
                or card.select_one("[class*='price']")
            )
            link_el  = (
                card.select_one(".card-link")
                or card.select_one("a[href*='/europarcs']")
                or card.select_one("a[href]")
            )
            loc_els  = (
                card.select(".card-location")
                or card.select("[class*='location']")
            )
            img_el   = card.select_one("img")
            bed_el   = (
                card.select_one(".bedroom-count")
                or card.select_one("[class*='bedroom']")
            )
            pers_el  = (
                card.select_one(".person-count")
                or card.select_one("[class*='person']")
            )

            if not link_el:
                continue
            href  = link_el.get("href", "")
            url_l = href if href.startswith("http") else base + href
            if not url_l.startswith("http"):
                continue

            full_text = card.get_text(" ", strip=True)
            title     = title_el.get_text(strip=True) if title_el else full_text[:60]
            price_str = price_el.get_text(strip=True) if price_el else full_text
            price     = extract_price(price_str)
            # "Vanaf € X,-" → prijs kan "Vanaf" prefix hebben
            location  = loc_els[-1].get_text(strip=True) if loc_els else ""
            image     = img_el.get("src", "") if img_el else ""
            bedrooms  = _safe_int(bed_el.get_text()) if bed_el else None
            persons   = _safe_int(pers_el.get_text()) if pers_el else None

            # Fallback: haal slaapk./pers. uit tekst (notatie: "2 slaapk.", "4 pers.")
            if bedrooms is None:
                bm = re.search(r"(\d+)\s*slaapk", full_text, re.IGNORECASE)
                bedrooms = int(bm.group(1)) if bm else None
            if persons is None:
                pm = re.search(r"(\d+)\s*pers\b", full_text, re.IGNORECASE)
                persons = int(pm.group(1)) if pm else None

            listings.append({
                "source":   "EuroParcs Makelaardij",
                "title":    title,
                "url":      url_l,
                "price":    price,
                "bedrooms": bedrooms,
                "persons":  persons,
                "location": location,
                "image":    image,
                "sold":     is_sold(full_text),
                "raw":      full_text[:200],
            })
            new_on_page += 1

        if new_on_page == 0:
            break
        page += 1
        time.sleep(0.8)

    log.info("EuroParcs Makelaardij: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Veluwechalets.nl
# ════════════════════════════════════════════════════════════

def scrape_veluwechalets() -> list[dict]:
    """
    Veluwechalets.nl: 6 pagina's, ~152 items.
    Structuur: <h3> titel, <h4> locatie, prijs in tekst, link /chalet/[slug].
    """
    log.info("Scrapen: Veluwechalets.nl")
    base     = "https://www.veluwechalets.nl"
    listings: list[dict] = []
    seen_urls: set[str]  = set()

    for page in range(1, 10):   # maximaal 10 pagina's
        url  = f"{base}/aanbod/chalets/{page}"
        soup = get_page(url)
        if not soup:
            break

        chalet_links = soup.find_all("a", href=re.compile(r"/chalet/"))
        if not chalet_links:
            break

        new_on_page = 0
        for a in chalet_links:
            href  = a.get("href", "")
            url_l = href if href.startswith("http") else base + href
            if url_l in seen_urls:
                continue
            seen_urls.add(url_l)
            new_on_page += 1

            # Kaart = parent artikel/div
            card      = a.find_parent(["article", "div", "li"]) or a
            full_text = card.get_text(" ", strip=True)

            h3  = card.find("h3") or a.find("h3")
            h4  = card.find("h4") or a.find("h4")
            img = card.find("img")

            title    = h3.get_text(strip=True) if h3 else href.rstrip("/").split("/")[-1].replace("-", " ").title()
            location = h4.get_text(strip=True) if h4 else "Veluwe"
            price    = extract_price(full_text)
            image    = img.get("src", "") if img else ""

            # Slaapkamers / personen: niet altijd op lijstpagina
            bed_m  = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)
            pers_m = re.search(r"(\d+)[- ]persoons|(\d+)\s*personen", full_text, re.IGNORECASE)

            listings.append({
                "source":   "Veluwechalets.nl",
                "title":    title,
                "url":      url_l,
                "price":    price,
                "bedrooms": int(bed_m.group(1)) if bed_m else None,
                "persons":  int(pers_m.group(1) or pers_m.group(2)) if pers_m else None,
                "location": f"{location}, Veluwe",
                "image":    image,
                "sold":     is_sold(full_text),
                "raw":      full_text[:200],
            })

        if new_on_page == 0:
            break
        time.sleep(0.8)

    log.info("Veluwechalets.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: UwTweedeHuisMakelaar.nl
# ════════════════════════════════════════════════════════════

def scrape_uwtweedehuismakelaar() -> list[dict]:
    """
    WordPress-site. Paginering: /aanbod/page/N/.
    Kaarten bevatten titel, prijs, slaapkamers, personen.
    """
    log.info("Scrapen: UwTweedeHuisMakelaar.nl")
    base     = "https://uwtweedehuismakelaar.nl"
    listings: list[dict] = []
    seen_urls: set[str]  = set()
    page = 1

    while True:
        url  = f"{base}/aanbod/" if page == 1 else f"{base}/aanbod/page/{page}/"
        soup = get_page(url)
        if not soup:
            break

        # WordPress: articles of divs met aanbod-links
        cards: list = []
        for a in soup.select("a[href*='/aanbod/']"):
            href = a.get("href", "")
            if href.rstrip("/") in {f"{base}/aanbod", "/aanbod"}:
                continue
            parent = (
                a.find_parent("article")
                or a.find_parent("li")
                or a.find_parent("div")
            )
            if parent and parent not in cards:
                cards.append(parent)

        if not cards:
            break

        new_on_page = 0
        for card in cards:
            link_el = card.select_one("a[href*='/aanbod/']")
            if not link_el:
                continue
            href  = link_el.get("href", "")
            url_l = href if href.startswith("http") else base + href
            if url_l in seen_urls or url_l.rstrip("/") == f"{base}/aanbod":
                continue
            seen_urls.add(url_l)
            new_on_page += 1

            full_text = card.get_text(" ", strip=True)
            title_el  = card.select_one("h2, h3, [class*='title']")
            img_el    = card.select_one("img")

            title    = title_el.get_text(strip=True) if title_el else full_text[:60]
            price    = extract_price(full_text)
            image    = img_el.get("src", "") if img_el else ""

            bed_m  = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)
            pers_m = re.search(r"(\d+)\s*personen", full_text, re.IGNORECASE)

            # Locatie: bijv. "EuroParcs ..., Maarn, Utrecht"
            loc_el   = card.select_one("[class*='location'],[class*='city'],[class*='place']")
            location = loc_el.get_text(strip=True) if loc_el else ""
            if not location:
                lm = re.search(
                    r",\s*([A-Za-z\-]+),\s*"
                    r"(Utrecht|Gelderland|Overijssel|Drenthe|Noord-Brabant|Limburg)",
                    full_text,
                )
                location = lm.group(0).strip(", ") if lm else title[:40]

            listings.append({
                "source":   "UwTweedeHuisMakelaar.nl",
                "title":    title,
                "url":      url_l,
                "price":    price,
                "bedrooms": int(bed_m.group(1)) if bed_m else None,
                "persons":  int(pers_m.group(1)) if pers_m else None,
                "location": location,
                "image":    image,
                "sold":     is_sold(full_text),
                "raw":      full_text[:200],
            })

        if new_on_page == 0:
            break
        page += 1
        time.sleep(0.8)

    log.info("UwTweedeHuisMakelaar.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Uw Buitenleven
# ════════════════════════════════════════════════════════════

def scrape_uwbuitenleven() -> list[dict]:
    """
    WordPress-site. Alle woningen op /aanbod/ (Laad meer = single page).
    Kaart: <a href="/aanbod/slug/"> wrapper met <h3> en prijs.
    """
    log.info("Scrapen: UwBuitenleven.nl")
    base = "https://www.uw-buitenleven.nl"
    soup = get_page(f"{base}/aanbod/")
    if not soup:
        return []

    listings: list[dict] = []
    seen_urls: set[str]  = set()

    for a in soup.select("a[href*='/aanbod/']"):
        href  = a.get("href", "")
        url_l = href if href.startswith("http") else base + href
        if url_l.rstrip("/") in {f"{base}/aanbod", base + "/aanbod"}:
            continue
        if url_l in seen_urls:
            continue
        seen_urls.add(url_l)

        full_text = a.get_text(" ", strip=True)
        h3        = a.find("h3")
        img       = a.find("img")

        title    = h3.get_text(strip=True) if h3 else full_text[:60]
        price    = extract_price(full_text)
        image    = img.get("src", "") if img else ""
        bed_m    = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)

        # Locatie: postcode + stad patroon of uit titel
        loc_m    = re.search(r"\d{4}\s*[A-Z]{2}\s+([A-Za-z\s]+)", full_text)
        location = loc_m.group(1).strip() if loc_m else title[:40]

        listings.append({
            "source":   "UwBuitenleven.nl",
            "title":    title,
            "url":      url_l,
            "price":    price,
            "bedrooms": int(bed_m.group(1)) if bed_m else None,
            "persons":  None,
            "location": location,
            "image":    image,
            "sold":     is_sold(full_text),
            "raw":      full_text[:200],
        })

    log.info("UwBuitenleven.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: TopParkenVerkoop.nl
# ════════════════════════════════════════════════════════════

def scrape_topparkenverkoop() -> list[dict]:
    log.info("Scrapen: TopParkenVerkoop.nl")
    base = "https://www.topparkenverkoop.nl"
    soup = get_page(f"{base}/aanbod-vakantiewoningen")
    if not soup:
        return []

    listings: list[dict] = []
    seen_urls: set[str]  = set()

    # Zoek naar alle links die naar individuele woningpagina's wijzen
    for a in soup.find_all("a", href=re.compile(r"/aanbod-vakantiewoningen/.+")):
        href  = a.get("href", "")
        url_l = href if href.startswith("http") else base + href
        if url_l in seen_urls:
            continue
        seen_urls.add(url_l)

        card      = a.find_parent(["article", "li", "div"]) or a
        full_text = card.get_text(" ", strip=True)
        price     = extract_price(full_text)
        img       = card.find("img")
        title_el  = card.find(["h2", "h3", "h4"]) or a
        title     = title_el.get_text(strip=True)[:80]

        bed_m  = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)
        pers_m = re.search(r"(\d+)\s*personen", full_text, re.IGNORECASE)
        loc_m  = re.search(
            r"([A-Za-z\s\-]+),\s*(Gelderland|Overijssel|Drenthe|Utrecht|Noord-Brabant|Limburg)",
            full_text,
        )
        location = loc_m.group(0) if loc_m else title[:40]

        listings.append({
            "source":   "TopParkenVerkoop.nl",
            "title":    title,
            "url":      url_l,
            "price":    price,
            "bedrooms": int(bed_m.group(1)) if bed_m else None,
            "persons":  int(pers_m.group(1)) if pers_m else None,
            "location": location,
            "image":    img.get("src", "") if img else "",
            "sold":     is_sold(full_text),
            "raw":      full_text[:200],
        })

    log.info("TopParkenVerkoop.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Vakantiemakelaar.nl
# ════════════════════════════════════════════════════════════

def scrape_vakantiemakelaar() -> list[dict]:
    log.info("Scrapen: Vakantiemakelaar.nl")
    base = "https://www.vakantiemakelaar.nl"
    # Probeer de meest waarschijnlijke listing-URLs
    for path in ["/aanbod", "/woningen", "/te-koop", "/"]:
        soup = get_page(f"{base}{path}")
        if not soup:
            continue

        listings: list[dict] = []
        seen_urls: set[str]  = set()

        # Zoek naar kaartjes met een prijs en een interne link
        for a in soup.find_all("a", href=True):
            href  = a.get("href", "")
            url_l = href if href.startswith("http") else base + href
            if not url_l.startswith(base):
                continue
            if url_l in seen_urls or url_l == base + path:
                continue

            card      = a.find_parent(["article", "li", "div"]) or a
            full_text = card.get_text(" ", strip=True)
            price     = extract_price(full_text)
            if not price:
                continue

            seen_urls.add(url_l)
            img       = card.find("img")
            title_el  = card.find(["h2", "h3", "h4"])
            title     = title_el.get_text(strip=True)[:80] if title_el else full_text[:60]
            bed_m     = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)
            pers_m    = re.search(
                r"(\d+)[- ]persoons|(\d+)\s*personen", full_text, re.IGNORECASE
            )
            loc_m = re.search(
                r"([A-Za-z\s\-]+),\s*(Gelderland|Overijssel|Drenthe|Utrecht|Noord-Brabant|Limburg)",
                full_text,
            )
            location  = loc_m.group(0) if loc_m else title[:40]

            listings.append({
                "source":   "Vakantiemakelaar.nl",
                "title":    title,
                "url":      url_l,
                "price":    price,
                "bedrooms": int(bed_m.group(1)) if bed_m else None,
                "persons":  int(pers_m.group(1) or pers_m.group(2)) if pers_m else None,
                "location": location,
                "image":    img.get("src", "") if img else "",
                "sold":     is_sold(full_text),
                "raw":      full_text[:200],
            })

        if listings:
            log.info("Vakantiemakelaar.nl: %d woningen (via %s)", len(listings), path)
            return listings

    log.warning("Vakantiemakelaar.nl: geen woningen gevonden")
    return []


# ════════════════════════════════════════════════════════════
#  Scraper: CenterParcs Vastgoed
# ════════════════════════════════════════════════════════════

def scrape_centerparcs_vastgoed() -> list[dict]:
    log.info("Scrapen: CenterParcs Vastgoed")
    base = "https://www.centerparcs-vastgoed.nl"
    soup = get_page(f"{base}/recreatiewoningen-te-koop")
    if not soup:
        return []

    listings: list[dict] = []
    seen_urls: set[str]  = set()

    for a in soup.find_all("a", href=True):
        href  = a.get("href", "")
        url_l = href if href.startswith("http") else base + href
        if not url_l.startswith(base) or url_l in seen_urls:
            continue

        card      = a.find_parent(["article", "li", "div"]) or a
        full_text = card.get_text(" ", strip=True)
        price     = extract_price(full_text)
        if not price:
            continue

        seen_urls.add(url_l)
        img      = card.find("img")
        title_el = card.find(["h2", "h3", "h4"])
        title    = title_el.get_text(strip=True)[:80] if title_el else full_text[:60]
        bed_m    = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)
        pers_m   = re.search(r"(\d+)\s*personen|(\d+)[- ]persoons", full_text, re.IGNORECASE)

        listings.append({
            "source":   "CenterParcs Vastgoed",
            "title":    title,
            "url":      url_l,
            "price":    price,
            "bedrooms": int(bed_m.group(1)) if bed_m else None,
            "persons":  int(pers_m.group(1) or pers_m.group(2)) if pers_m else None,
            "location": title[:40],
            "image":    img.get("src", "") if img else "",
            "sold":     is_sold(full_text),
            "raw":      full_text[:200],
        })

    log.info("CenterParcs Vastgoed: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Jaap.nl
# ════════════════════════════════════════════════════════════

def scrape_jaap() -> list[dict]:
    log.info("Scrapen: Jaap.nl")
    base = "https://www.jaap.nl"
    url  = (
        f"{base}/koophuizen/recreatiewoningen/nederland/"
        f"?prijs-van=0&prijs-tot={config.MAX_PRICE}"
    )
    soup = get_page(url)
    if not soup:
        return []

    listings: list[dict] = []
    cards = soup.select("article.property-list-item, .property-item, article")

    for card in cards:
        link_el  = card.select_one("a[href]")
        title_el = card.select_one("h2, h3, .title, [class*='title']")
        price_el = card.select_one("[class*='price'], .price")
        img_el   = card.select_one("img")

        if not link_el:
            continue
        href  = link_el.get("href", "")
        url_l = href if href.startswith("http") else base + href
        title = title_el.get_text(" ", strip=True) if title_el else ""
        price = extract_price(price_el.get_text() if price_el else card.get_text())
        image = img_el.get("src", "") if img_el else ""
        if not url_l.startswith("http") or not price:
            continue

        full_text = card.get_text(" ", strip=True)
        loc_el    = card.select_one(
            "[class*='location'], [class*='city'], [class*='address']"
        )
        location  = loc_el.get_text(strip=True) if loc_el else title[:40]

        listings.append({
            "source":   "Jaap.nl",
            "title":    title[:80],
            "url":      url_l,
            "price":    price,
            "bedrooms": None,
            "persons":  None,
            "location": location,
            "image":    image,
            "sold":     is_sold(full_text),
            "raw":      full_text[:200],
        })

    log.info("Jaap.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Huislijn.nl  (403 op standaard user-agent; probeer met referer)
# ════════════════════════════════════════════════════════════

def scrape_huislijn() -> list[dict]:
    log.info("Scrapen: Huislijn.nl")
    base = "https://www.huislijn.nl"
    provinces = [
        "gelderland", "overijssel", "drenthe", "limburg", "noord-brabant"
    ]
    listings: list[dict] = []
    seen_urls: set[str]  = set()

    for province in provinces:
        url  = (
            f"{base}/koopwoning/recreatiewoning/nederland/{province}"
            f"?pricemax={config.MAX_PRICE}"
        )
        try:
            resp = SESSION.get(
                url, timeout=15,
                headers={**HEADERS, "Referer": "https://www.google.nl/"},
            )
            if resp.status_code == 403:
                log.warning("Huislijn.nl: toegang geblokkeerd (403)")
                return []
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            log.warning("Huislijn.nl: %s", exc)
            return []

        cards = soup.select(
            ".search-result-item, [class*='result-item'], "
            ".object-list-item, article"
        )
        for card in cards:
            link_el  = card.select_one("a[href]")
            title_el = card.select_one("h2, h3, [class*='title'], [class*='address']")
            price_el = card.select_one("[class*='price'], .price, [class*='koopprijs']")
            img_el   = card.select_one("img")

            if not link_el:
                continue
            href  = link_el.get("href", "")
            url_l = href if href.startswith("http") else base + href
            if url_l in seen_urls:
                continue
            title = title_el.get_text(" ", strip=True) if title_el else ""
            price = extract_price(price_el.get_text() if price_el else card.get_text())
            if not url_l.startswith("http") or not price or not title:
                continue
            seen_urls.add(url_l)

            full_text = card.get_text(" ", strip=True)
            loc_el    = card.select_one("[class*='city'],[class*='location'],[class*='place']")
            location  = loc_el.get_text(strip=True) if loc_el else province.title()
            bed_m     = re.search(r"(\d+)\s*slaapkamer", full_text, re.IGNORECASE)
            pers_m    = re.search(
                r"(\d+)[- ]persoons|(\d+)\s*personen", full_text, re.IGNORECASE
            )

            listings.append({
                "source":   "Huislijn.nl",
                "title":    title[:80],
                "url":      url_l,
                "price":    price,
                "bedrooms": int(bed_m.group(1)) if bed_m else None,
                "persons":  int(pers_m.group(1) or pers_m.group(2)) if pers_m else None,
                "location": location,
                "image":    img_el.get("src", "") if img_el else "",
                "sold":     is_sold(full_text),
                "raw":      full_text[:200],
            })
        time.sleep(0.8)

    log.info("Huislijn.nl: %d woningen", len(listings))
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Roompot (stub – geen publieke verkooppagina)
# ════════════════════════════════════════════════════════════

def scrape_roompot() -> list[dict]:
    return []


# ════════════════════════════════════════════════════════════
#  E-mail via SMTP
# ════════════════════════════════════════════════════════════

def _smtp_ready() -> bool:
    if not config.EMAIL_TO:
        log.warning("E-mail: geen ontvangers ingesteld")
        return False
    if not config.SMTP_PASSWORD or "JOUW" in config.SMTP_PASSWORD:
        log.warning("E-mail: SMTP_PASSWORD niet ingesteld – sla e-mail over")
        return False
    return True


def _send_raw(subject: str, html_body: str, text_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_FROM
    msg["To"]      = ", ".join(config.EMAIL_TO)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_bytes())
        log.info("E-mail verstuurd naar: %s", ", ".join(config.EMAIL_TO))
    except Exception as exc:
        log.error("E-mail versturen mislukt: %s", exc)

def _calc_price_trend(history: list[dict]) -> str:
    """Geeft 'down', 'up', 'stable' of 'unknown' terug op basis van prijshistorie."""
    prices = [h["price"] for h in history if h.get("price")]
    if len(prices) < 2:
        return "unknown"
    if prices[-1] < prices[-2]:
        return "down"
    if prices[-1] > prices[-2]:
        return "up"
    return "stable"
def _fmt_price(price: int | None) -> str:
    return f"€ {price:,.0f}".replace(",", ".") if price else "Prijs onbekend"


def build_email_html(new_listings: list[dict]) -> str:
    sorted_listings = sorted(new_listings, key=lambda l: l.get("price") or 0, reverse=True)
    count  = len(sorted_listings)
    plural = "en" if count > 1 else ""
    ts     = datetime.now().strftime("%d %B %Y om %H:%M")

    rows_html = ""
    for i in range(0, len(sorted_listings), 2):
        pair = sorted_listings[i:i + 2]
        cells = ""
        for l in pair:
            price    = _fmt_price(l.get("price"))
            bedrooms = l.get("bedrooms") or "?"
            persons  = l.get("persons")  or "?"
            location = l.get("location") or "Onbekend"
            title    = (l.get("title") or "Onbekend")[:55]
            source   = l.get("source", "")
            img      = l.get("image", "")

            img_html = (
                f'<img src="{img}" alt="" width="100%" '
                f'style="display:block;width:100%;height:140px;object-fit:cover;'
                f'border-radius:8px 8px 0 0;">'
                if img else
                # Placeholder: donkere tekst op lichtgrijze achtergrond
                '<div style="width:100%;height:80px;background:#dee2e6;color:#6c757d;'
                'border-radius:8px 8px 0 0;text-align:center;font-size:2rem;'
                'line-height:80px;">&#127968;</div>'
            )
            # Kaart: witte achtergrond, ALLE tekst expliciet donker gekleurd
            cells += f"""
              <td width="50%" valign="top" style="padding:6px;vertical-align:top;">
                <table width="100%" cellpadding="0" cellspacing="0"
                       style="border-collapse:collapse;background:#ffffff;
                              border-radius:10px;
                              box-shadow:0 2px 8px rgba(0,0,0,.15);
                              overflow:hidden;">
                  <tr><td style="padding:0;font-size:0;">{img_html}</td></tr>
                  <tr><td style="padding:14px;background:#ffffff;">
                    <!-- Bron-label: donkergroen op wit -->
                    <p style="margin:0 0 4px;font-size:10px;font-weight:700;
                               color:#1b4332;text-transform:uppercase;
                               letter-spacing:.05em;font-family:Arial,sans-serif;">
                      {source}</p>
                    <!-- Titel: bijna-zwart op wit -->
                    <p style="margin:0 0 8px;font-size:14px;font-weight:700;
                               color:#1a1a1a;line-height:1.4;
                               font-family:Arial,sans-serif;">
                      {title}</p>
                    <!-- Prijs: donkergroen op wit -->
                    <p style="margin:0 0 10px;font-size:18px;font-weight:800;
                               color:#1b4332;font-family:Arial,sans-serif;">
                      {price}</p>
                    <!-- Details: donkergrijs op wit -->
                    <p style="margin:0 0 3px;font-size:12px;color:#333333;
                               font-family:Arial,sans-serif;">
                      {location}</p>
                    <p style="margin:0 0 3px;font-size:12px;color:#333333;
                               font-family:Arial,sans-serif;">
                      {bedrooms} slaapkamers</p>
                    <p style="margin:0 0 12px;font-size:12px;color:#333333;
                               font-family:Arial,sans-serif;">
                      {persons} personen</p>
                    <!-- Knop: witte tekst op donkergroen -->
                    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                      <tr><td align="center"
                               style="background:#2d6a4f;border-radius:6px;padding:9px 6px;">
                        <a href="{WEBSITE_URL}"
                           style="color:#ffffff;text-decoration:none;font-size:12px;
                                  font-weight:700;font-family:Arial,sans-serif;
                                  display:block;">Bekijk op website &rarr;</a>
                      </td></tr>
                    </table>
                  </td></tr>
                </table>
              </td>"""
        if len(pair) == 1:
            cells += '<td width="50%">&nbsp;</td>'
        rows_html += f"<tr>{cells}</tr>"

    max_price_fmt = f"{config.MAX_PRICE:,}".replace(",", ".")

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <!-- Forceer light-mode in ondersteunde clients (Apple Mail, Outlook 365) -->
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <style>
    /* Forceer light-mode: voorkom dark-mode omzetting door e-mailclient */
    :root {{ color-scheme: light only; }}
    body  {{ background-color:#f8f9fa !important; color:#212529 !important; }}
  </style>
</head>
<!--[if mso]>
<body bgcolor="#f8f9fa" style="background-color:#f8f9fa;color:#212529;
      font-family:Arial,sans-serif;margin:0;padding:0;">
<![endif]-->
<body style="margin:0;padding:0;background-color:#f8f9fa;color:#212529;
             font-family:Arial,Helvetica,sans-serif;"
      bgcolor="#f8f9fa">

  <!-- Buitenste tabel voor maximale e-mail-clientcompatibiliteit -->
  <table width="100%" cellpadding="0" cellspacing="0"
         style="border-collapse:collapse;background-color:#f8f9fa;"
         bgcolor="#f8f9fa">
    <tr><td align="center" style="padding:24px 12px;">

      <!-- Wrapper: max 640px -->
      <table width="640" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;max-width:640px;width:100%;">

        <!-- ── Header: groene gradient ── -->
        <tr>
          <td align="center"
              style="background-color:#2d6a4f;border-radius:12px 12px 0 0;
                     padding:28px 24px;text-align:center;"
              bgcolor="#2d6a4f">
            <h1 style="margin:0 0 6px;font-size:22px;font-weight:700;
                        color:#ffffff;font-family:Arial,sans-serif;line-height:1.3;">
              Vakantiewoningen Te Koop
            </h1>
            <p style="margin:0 0 20px;font-size:14px;color:#d8f3dc;
                       font-family:Arial,sans-serif;">
              {count} nieuwe woning{plural} gevonden op {ts}
            </p>
            <!-- CTA button: oranje, witte tekst -->
            <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0 auto;">
              <tr><td align="center"
                       style="background-color:#f4a261;border-radius:50px;
                              padding:14px 36px;"
                       bgcolor="#f4a261">
                <a href="{WEBSITE_URL}"
                   style="color:#ffffff;text-decoration:none;font-size:16px;
                          font-weight:700;font-family:Arial,sans-serif;
                          white-space:nowrap;">
                  Bekijk {count} nieuwe woningen &rarr;
                </a>
              </td></tr>
            </table>
          </td>
        </tr>

        <!-- ── Woningkaarten: lichtgrijze achtergrond ── -->
        <tr>
          <td style="background-color:#f8f9fa;padding:12px 0;"
              bgcolor="#f8f9fa">
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse;">
              {rows_html}
            </table>
          </td>
        </tr>

        <!-- ── Footer ── -->
        <tr>
          <td align="center"
              style="background-color:#e9ecef;border-radius:0 0 12px 12px;
                     padding:14px 20px;font-size:12px;color:#495057;
                     font-family:Arial,sans-serif;"
              bgcolor="#e9ecef">
            Project Vacay Cabin &middot; max &euro;{max_price_fmt} &middot;
            min {config.MIN_BEDROOMS} slaapkamers &middot; min {config.MIN_PERSONS} personen<br>
            <a href="{WEBSITE_URL}"
               style="color:#2d6a4f;text-decoration:none;font-family:Arial,sans-serif;">
              {WEBSITE_URL}
            </a>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_email(new_listings: list[dict]) -> None:
    if not _smtp_ready():
        return
    count    = len(new_listings)
    plural   = "en" if count > 1 else ""
    now      = datetime.now()
    date_str = f"{now.day}-{now.month}-{str(now.year)[2:]}"
    subject  = f"Project Vacay Cabin - {count} nieuwe woning{plural} gevonden - {date_str}"
    html_body = build_email_html(new_listings)
    lines = [f"Nieuwe vakantiewoningen ({count}):\n"]
    for l in sorted(new_listings, key=lambda x: x.get("price") or 0, reverse=True):
        lines.append(
            f"- {l['title']} | {_fmt_price(l.get('price'))} | "
            f"{l.get('bedrooms','?')} slpk | {l.get('persons','?')} pers"
        )
    _send_raw(subject, html_body, "\n".join(lines))


def send_alert_email(days_since: int, last_new_date: str) -> None:
    if not _smtp_ready():
        return
    now      = datetime.now()
    date_str = f"{now.day}-{now.month}-{str(now.year)[2:]}"
    subject  = (
        f"Project Vacay Cabin - Geen nieuwe woningen in {days_since} dagen - {date_str}"
    )
    html_body = f"""<!DOCTYPE html>
<html lang="nl"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8f9fa;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:600px;margin:2rem auto;padding:0 1rem;">
    <div style="background:linear-gradient(135deg,#6c757d,#495057);
                border-radius:12px 12px 0 0;padding:2rem 1.5rem;text-align:center;">
      <h1 style="margin:0 0 .5rem;font-size:1.35rem;color:#ffffff;">
        📭 Geen nieuwe woningen in {days_since} dagen
      </h1>
      <p style="margin:0;font-size:.875rem;color:#dee2e6;">
        Laatste nieuwe woning: {last_new_date}
      </p>
    </div>
    <div style="background:#ffffff;padding:1.5rem;border:1px solid #dee2e6;">
      <p style="color:#212529;margin:0 0 1rem;">
        De scraper draait dagelijks maar vond de afgelopen
        <strong>{days_since} dagen</strong> geen nieuwe woningen die aan de filters voldoen.
      </p>
      <ul style="color:#343a40;margin:0 0 1rem;padding-left:1.5rem;">
        <li>Er is tijdelijk geen nieuw aanbod</li>
        <li>De filters zijn te streng (prijs / slaapkamers / regio)</li>
        <li>Een of meer bronnen zijn tijdelijk niet bereikbaar</li>
      </ul>
      <a href="{WEBSITE_URL}"
         style="display:inline-block;background:#2d6a4f;color:#ffffff;
                text-decoration:none;padding:10px 24px;border-radius:8px;
                font-size:.9rem;font-weight:600;">
        Bekijk huidige woningen op de website →
      </a>
    </div>
    <div style="background:#e9ecef;border-radius:0 0 12px 12px;
                padding:.75rem 1.5rem;font-size:.75rem;color:#6c757d;text-align:center;">
      Project Vacay Cabin · automatisch gegenereerd
    </div>
  </div>
</body></html>"""
    _send_raw(
        subject, html_body,
        f"Geen nieuwe woningen in {days_since} dagen.\n"
        f"Laatste nieuwe woning: {last_new_date}\nWebsite: {WEBSITE_URL}",
    )


# ════════════════════════════════════════════════════════════
#  Hoofdlogica
# ════════════════════════════════════════════════════════════

def run() -> None:
    start_time = time.monotonic()

    log.info("=" * 60)
    log.info("Vakantiewoning scraper gestart  (v6)")
    log.info("Filters: max €%s · min %d slaapkamers · min %d personen",
             f"{config.MAX_PRICE:,}", config.MIN_BEDROOMS, config.MIN_PERSONS)
    log.info("=" * 60)

    # ── Geocoding-cache laden ─────────────────────────────────
    _load_geocode_cache()

    # ── Geblokkeerde URLs laden uit Supabase ─────────────────
    blocked_url_keys = _load_blocked_urls()

    known, meta = load_known()
    log.info("Bekende woningen: %d", len(known))

    # ── Stap 1: scrapers PARALLEL uitvoeren ──────────────────
    scrapers = [
        scrape_recreatievastgoed,
        scrape_marktplaats,
        scrape_vakantiehuistekoop,
        scrape_landalmakelaardij,
        scrape_recreatiewoningentekoop,
        scrape_europarcsmakelaardij,
        scrape_veluwechalets,
        scrape_uwtweedehuismakelaar,
        scrape_uwbuitenleven,
        scrape_topparkenverkoop,
        scrape_vakantiemakelaar,
        scrape_centerparcs_vastgoed,
        scrape_jaap,
        scrape_huislijn,
        scrape_roompot,
    ]

    all_raw: list[dict] = []
    # Telt ruw gevonden woningen per bron (voor run_stats)
    scraper_counts: dict[str, int] = {}
    scraper_errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_name = {
            executor.submit(fn): fn.__name__ for fn in scrapers
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results = future.result()
                all_raw.extend(results)
                # Groepeer per bron-naam voor stats
                for l in results:
                    src = l.get("source", name)
                    scraper_counts[src] = scraper_counts.get(src, 0) + 1
                log.info("%-35s %d woningen", name, len(results))
            except Exception as exc:
                log.error("Scraper %s crashte: %s", name, exc)
                scraper_errors[name] = str(exc)

    log.info("Totaal gescraped (ongefilterd): %d", len(all_raw))

    # ── Bijhouden actieve bronnen en geziene URLs ─────────────
    scraped_url_keys: set[str] = set()
    active_sources:   set[str] = set()
    for l in all_raw:
        k = url_key(l.get("url", ""))
        if k:
            scraped_url_keys.add(k)
        if l.get("source"):
            active_sources.add(l["source"])

    # ── Stap 2: regio + prijs + personen filter ───────────────
    def in_region(l: dict) -> bool:
        return is_in_region(" ".join([
            l.get("location") or "", l.get("title") or "", l.get("raw") or ""
        ]))

    pre = [
        l for l in all_raw
        if not l.get("sold")
        and (l.get("price") is None or l["price"] <= config.MAX_PRICE)
        and (l.get("persons") is None or l["persons"] >= config.MIN_PERSONS)
        and in_region(l)
    ]
    log.info("Na regio + prijs + personen filter: %d", len(pre))

    # ── Stap 3a: coördinaten van bekende listings kopiëren ────
    # Doe dit VOOR geocoding zodat reeds bekende coords niet opnieuw
    # worden opgehaald (bespaart Nominatim-aanvragen).
    for l in pre:
        key = url_key(l.get("url", "")) or f"{l['source']}::{l['title']}"
        ex  = known.get(key, {})
        if ex.get("lat") and not l.get("lat"):
            l["lat"] = ex["lat"]
            l["lng"] = ex.get("lng")

    # ── Stap 3b: slaapkamers ophalen van detailpagina's ───────
    without_beds = [l for l in pre if l.get("bedrooms") is None]
    if without_beds:
        log.info("Detailpagina's ophalen voor %d woningen (slaapkamers)…",
                 len(without_beds))
        pre = enrich_with_details(pre)

    # ── Stap 3c: geocoding voor listings zonder coördinaten ───
    # Loopt altijd — onafhankelijk van of beds ontbraken.
    needs_geo = [l for l in pre if l.get("lat") is None and l.get("location")]
    if needs_geo:
        log.info("Geocoding voor %d woningen zonder coördinaten…", len(needs_geo))
        for l in needs_geo:
            coords = geocode(l["location"])   # cache-aware
            if coords:
                l["lat"], l["lng"] = coords
                log.debug("Geocoded: %s → (%.4f, %.4f)", l["location"], *coords)
            # sleep alleen als we écht een Nominatim-aanvraag deden
            # (geocode() sleept zelf al bij cache-miss)

    # ── Stap 4: slaapkamer filter + volledigheidscheck ────────
    filtered = [l for l in pre if passes_filters(l)]
    complete = [l for l in filtered if is_complete(l)]
    skipped  = len(filtered) - len(complete)
    if skipped:
        log.info("Onvolledige woningen overgeslagen: %d", skipped)
filtered = _dedup_cross_source(complete)
log.info("Na cross-source deduplicatie: %d woningen", len(filtered))

    # ── Stap 4b: geblokkeerde listings verwijderen ───────────
    # Listings waarvan de url_key in blocked_urls staat worden behandeld
    # alsof ze al bekend en verwijderd zijn — ze verschijnen nooit als nieuw.
    if blocked_url_keys:
        before_blocked = len(filtered)
        filtered = [
            l for l in filtered
            if url_key(l.get("url", "")) not in blocked_url_keys
        ]
        skipped_blocked = before_blocked - len(filtered)
        if skipped_blocked:
            log.info("Geblokkeerde woningen overgeslagen: %d", skipped_blocked)

    # ── Stap 5: deduplicatie + prijshistorie + first_seen ─────
    today         = datetime.now().strftime("%Y-%m-%d")
    new_listings: list[dict] = []
    updated_known = dict(known)

    for l in filtered:
        key      = url_key(l.get("url", "")) or f"{l['source']}::{l['title']}"
        existing = updated_known.get(key, {})

        # Prijshistorie
        price_hist    = list(existing.get("price_history", []))
        current_price = l.get("price")
        if not price_hist or price_hist[-1].get("price") != current_price:
            price_hist.append({"date": today, "price": current_price})

        # Coördinaten bewaren indien al bekend
        if not l.get("lat") and existing.get("lat"):
            l["lat"] = existing["lat"]
            l["lng"] = existing.get("lng")

        l_stored = {
            **l,
            "offline":       False,
            "first_seen":    existing.get("first_seen", today),
            "price_history": price_hist,
            "price_trend":   _calc_price_trend(price_hist),
        }

        if key not in updated_known:
            new_listings.append(l)
            log.info(
                "NIEUW ★  %-28s | %-38s | %s slpk | %s pers | %s",
                l["source"], l["title"][:38],
                l.get("bedrooms") or "?",
                l.get("persons") or "?",
                _fmt_price(l.get("price")),
            )

        updated_known[key] = l_stored

    log.info("Nieuwe woningen deze run: %d", len(new_listings))

    # ── Stap 5b: offline-status bijwerken ────────────────────
    offline_count = 0
    for key, listing in updated_known.items():
        src = listing.get("source", "")
        if src in active_sources:
            was_seen = key in scraped_url_keys
            if not was_seen and not listing.get("offline"):
                offline_count += 1
            listing["offline"] = not was_seen
    if offline_count:
        log.info("%d woningen gemarkeerd als offline", offline_count)

    # ── Stap 5c: screenshots voor nieuwe woningen zonder foto ─
    # Overslaan als SKIP_SCREENSHOTS=1 is gezet in de omgeving
   is_monday = datetime.now().weekday() == 0
    if os.environ.get("SKIP_SCREENSHOTS") == "1" or not is_monday:
        log.info("Screenshots overgeslagen (niet maandag of SKIP_SCREENSHOTS=1)")
    else:
        screenshots_taken = 0
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        for l in new_listings:
            if screenshots_taken >= 5 or l.get("image"):
                continue
            wurl = l.get("url", "")
            if not wurl.startswith("http"):
                continue
            safe_name = re.sub(r"[^a-z0-9]", "_", url_key(wurl))[:60] + ".png"
            save_path = SCREENSHOTS_DIR / safe_name
            if not save_path.exists() and take_screenshot(wurl, save_path):
                l["image"] = f"screenshots/{safe_name}"
                ukey = url_key(wurl)
                if ukey in updated_known:
                    updated_known[ukey]["image"] = l["image"]
                screenshots_taken += 1

    # ── Bron-zero-streak bijwerken ────────────────────────────
    meta.setdefault("source_zero_streak", {})
    streak = meta["source_zero_streak"]
    warned_sources = []
    for src, cnt in scraper_counts.items():
        if cnt == 0:
            streak[src] = streak.get(src, 0) + 1
            if streak[src] == 5:
                warned_sources.append(src)
        else:
            streak[src] = 0
    if warned_sources:
        log.warning(
            "Bronnen met 5 runs op rij zonder resultaat: %s",
            ", ".join(warned_sources)
        )
    # ── Stap 6: 14-daagse alert ───────────────────────────────
    last_new_str = meta.get("last_new_found", "")
    if new_listings:
        meta["last_new_found"] = today
    elif last_new_str:
        try:
            days_since = (
                datetime.now() - datetime.strptime(last_new_str, "%Y-%m-%d")
            ).days
            if days_since >= ALERT_DAYS:
                send_alert_email(days_since, last_new_str)
        except ValueError:
            pass

    # ── Stap 7: opruimen — verwijder listings >90 dagen offline ─
    cutoff       = datetime.now() - timedelta(days=OFFLINE_DAYS)
    before_count = len(updated_known)
    updated_known = {
        key: listing
        for key, listing in updated_known.items()
        if not (
            listing.get("offline")
            and listing.get("first_seen")
            and datetime.strptime(listing["first_seen"], "%Y-%m-%d") < cutoff
        )
    }
    removed = before_count - len(updated_known)
    if removed:
        log.info("Opruimen: %d woningen verwijderd (>%d dagen offline)",
                 removed, OFFLINE_DAYS)

    # ── Stap 8: run-statistieken samenstellen ────────────────
    runtime = round(time.monotonic() - start_time, 1)
    run_stats = {
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "runtime_sec":     runtime,
        "total_scraped":   len(all_raw),
        "total_filtered":  len(filtered),
        "total_new":       len(new_listings),
        "per_source": {
            src: {
                "found":      scraper_counts.get(src, 0),
                "in_results": sum(1 for l in filtered      if l.get("source") == src),
                "new":        sum(1 for l in new_listings  if l.get("source") == src),
                "error": scraper_errors.get(src),
            }
            for src in sorted(scraper_counts.keys())
        },
    }
    log.info("Runtime: %.1f s | per_source-stats: %d bronnen",
             runtime, len(run_stats["per_source"]))

    # ── Stap 9: opslaan ──────────────────────────────────────
    save_known(updated_known, meta)

    # ── Stap 9b: upsert naar Supabase ────────────────────────
    new_url_keys_set = {url_key(l.get("url", "")) for l in new_listings}
    _upsert_to_supabase(updated_known, new_url_keys_set, blocked_url_keys)

    # ── Stap 10: data.json (alle woningen + stats) ───────────
    write_data_json(
        all_listings=list(updated_known.values()),
        new_listings=new_listings,
        run_stats=run_stats,
    )

    # ── Stap 11: e-mail sturen ───────────────────────────────
    if new_listings:
        send_email(new_listings)
    else:
        log.info("Geen nieuwe woningen – geen e-mail verstuurd.")

    log.info("Klaar in %.1f seconden.", runtime)


if __name__ == "__main__":
    run()
