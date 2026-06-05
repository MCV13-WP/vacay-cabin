#!/usr/bin/env python3
"""
Vakantiewoning Scraper
======================
Bronnen  : recreatievastgoed.nl · Marktplaats · vakantiehuistekoop.nl
           · Landal Makelaardij
Notificatie : HTML-e-mail via SMTP
Website  : docs/data.json → GitHub Pages (docs/index.html)
"""

import json
import logging
import re
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ════════════════════════════════════════════════════════════
#  Persistentie
# ════════════════════════════════════════════════════════════

def load_known() -> dict[str, dict]:
    """Laad alle eerder geziene woningen (key = genormaliseerde URL)."""
    path = Path(config.KNOWN_LISTINGS_FILE)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known(listings: dict[str, dict]) -> None:
    with open(config.KNOWN_LISTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False, indent=2)
    log.info("known_listings.json: %d woningen opgeslagen", len(listings))


def url_key(url: str) -> str:
    """
    Waterdichte, stabiele sleutel op basis van URL.
    Verwijdert trailing slashes, query-strings en fragmenten.
    """
    url = url.strip()
    # Verwijder fragment
    url = url.split("#")[0]
    # Verwijder query-string (bijv. ?utm_source=...)
    url = url.split("?")[0]
    # Verwijder trailing slash
    url = url.rstrip("/")
    return url.lower()


def write_data_json(
    all_listings: list[dict],
    new_listings: list[dict],
) -> None:
    """Schrijf docs/data.json voor de GitHub Pages website."""
    docs_dir = Path(config.DATA_JSON_FILE).parent
    docs_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "new_listings": new_listings,
        "all_listings": all_listings,
    }
    with open(config.DATA_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info("docs/data.json bijgewerkt (%d nieuw, %d totaal)",
             len(new_listings), len(all_listings))


# ════════════════════════════════════════════════════════════
#  Hulpfuncties
# ════════════════════════════════════════════════════════════

def extract_price(text: str) -> int | None:
    m = re.search(r"[€€]\s*([\d\.]+)", text)
    if m:
        return int(re.sub(r"\.", "", m.group(1)))
    return None


def get_page(url: str, timeout: int = 15) -> BeautifulSoup | None:
    try:
        resp = SESSION.get(url, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.warning("Fout bij ophalen %s: %s", url, exc)
        return None


def is_sold(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in config.SOLD_KEYWORDS)


def is_in_region(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in config.REGION_KEYWORDS)


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


def _safe_int(val: str) -> int | None:
    m = re.search(r"(\d+)", str(val).strip())
    return int(m.group(1)) if m else None


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

    city_t  = city.get_text(strip=True)  if city    else ""
    prov_t  = prov.get_text(strip=True)  if prov    else ""
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
        "source": "Recreatievastgoed.nl",
        "title": f"{city_t} – {prov_t}".strip(" –"),
        "url": full_url,
        "price": price,
        "bedrooms": None,
        "persons": persons,
        "location": location,
        "image": image,
        "sold": is_sold(full_text),
        "raw": full_text[:200],
    }


def enrich_with_details(listings: list[dict]) -> list[dict]:
    """Haal slaapkamers op van detailpagina voor woningen zonder slaapkamer-info."""
    if config.MIN_BEDROOMS <= 0:
        return listings
    for listing in listings:
        if listing.get("bedrooms") is None and listing.get("url"):
            soup = get_page(listing["url"])
            if soup:
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
                # Probeer ook foto
                if not listing.get("image"):
                    og = soup.find("meta", property="og:image")
                    if og:
                        listing["image"] = og.get("content", "")
                time.sleep(0.5)
    return listings


# ════════════════════════════════════════════════════════════
#  Scraper: Roompot (geen publieke API, blijft als stub)
# ════════════════════════════════════════════════════════════

def scrape_roompot() -> list[dict]:
    log.info("Scrapen: Roompot (geen publieke verkooppagina)")
    return []


# ════════════════════════════════════════════════════════════
#  Scraper: Marktplaats
# ════════════════════════════════════════════════════════════

def scrape_marktplaats() -> list[dict]:
    log.info("Scrapen: Marktplaats recreatiewoningen")
    listings: list[dict] = []
    base_tpl = (
        "https://www.marktplaats.nl/l/huizen-en-kamers/"
        "recreatiewoningen-te-koop/?PriceTo={max_price}&currentPage={page}"
    )
    seen_ids: set[str] = set()
    page = 0

    while True:
        url = base_tpl.format(max_price=config.MAX_PRICE, page=page)
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
            price = int(price_cents / 100) if price_cents else None
            city  = item.get("location", {}).get("cityName", "")
            desc  = item.get("description") or item.get("categorySpecificDescription") or ""
            title = item.get("title", "Onbekend")
            vip   = item.get("vipUrl", "")
            url_l = f"https://www.marktplaats.nl{vip}" if vip.startswith("/") else vip
            imgs  = item.get("imageUrls") or []
            image = imgs[0] if imgs else ""

            beds = _safe_int(m.group(1)) if (m := re.search(r"(\d+)\s*slaapkamer", desc, re.IGNORECASE)) else None
            pers_m = re.search(r"(\d+)[- ]persoons|(\d+)\s*personen", desc, re.IGNORECASE)
            persons = int(pers_m.group(1) or pers_m.group(2)) if pers_m else None

            listings.append({
                "source": "Marktplaats",
                "title": title,
                "url": url_l,
                "price": price,
                "bedrooms": beds,
                "persons": persons,
                "location": city,
                "image": image,
                "sold": bool(item.get("reserved", False)),
                "raw": f"{title} {city} {desc[:200]}",
            })

        if new_on_page == 0:
            break
        page += 1
        time.sleep(0.8)

    log.info("Marktplaats: %d woningen", len(listings))
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
        log.warning("vakantiehuistekoop.nl: niet bereikbaar")
        return []

    listings: list[dict] = []
    cards = [c for c in soup.select("[class*=property-card]") if "€" in c.get_text()]

    for card in cards:
        title_el = card.select_one(".property-card__title")
        price_el = card.select_one(".property-card__price")
        link_el  = card.select_one("a[href]")
        img_el   = card.select_one("img")

        title    = title_el.get_text(" ", strip=True) if title_el else "Onbekend"
        price    = extract_price(price_el.get_text() if price_el else "")
        href     = link_el["href"] if link_el else ""
        url      = href if href.startswith("http") else base + href
        image    = img_el.get("src", "") if img_el else ""

        icon_vals = [v.get_text(strip=True) for v in card.select(".property-card__icon-val")]
        persons   = _parse_persons_range(icon_vals[0] if icon_vals else "")
        bedrooms  = _safe_int(icon_vals[1] if len(icon_vals) > 1 else "")

        listings.append({
            "source": "VakantiehuisTekoop.nl",
            "title": title,
            "url": url,
            "price": price,
            "bedrooms": bedrooms,
            "persons": persons,
            "location": title,
            "image": image,
            "sold": is_sold(card.get_text(" ", strip=True)),
            "raw": card.get_text(" ", strip=True)[:200],
        })

    log.info("vakantiehuistekoop.nl: %d woningen", len(listings))
    return listings


def _parse_persons_range(val: str) -> int | None:
    """'6 - 8' → 8 (neem maximum van de range)."""
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", val.strip())
    if m:
        return int(m.group(2))
    m2 = re.search(r"(\d+)", val.strip())
    return int(m2.group(1)) if m2 else None


# ════════════════════════════════════════════════════════════
#  Scraper: Landal Makelaardij
# ════════════════════════════════════════════════════════════

_LM_TARGET_PROVINCES = {
    "gelderland", "overijssel", "utrecht", "drenthe",
    "friesland", "flevoland", "noord-brabant", "limburg", "zeeland",
}


def scrape_landalmakelaardij() -> list[dict]:
    log.info("Scrapen: Landal Makelaardij")
    soup_xml = get_page("https://www.landalmakelaardij.nl/vm_object_cpt-sitemap.xml")
    if not soup_xml:
        log.warning("Landal Makelaardij: sitemap niet bereikbaar")
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

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else url.rstrip("/").split("/")[-1]

    og_img = soup.find("meta", property="og:image")
    image  = og_img.get("content", "") if og_img else ""

    return {
        "source": "Landal Makelaardij",
        "title": title,
        "url": url,
        "price": price,
        "bedrooms": bedrooms,
        "persons": persons,
        "location": province.replace("-", " ").title(),
        "image": image,
        "sold": is_sold(text),
        "raw": text[:200],
    }


# ════════════════════════════════════════════════════════════
#  E-mail via SMTP
# ════════════════════════════════════════════════════════════

WEBSITE_URL = "https://mcv13-wp.github.io/vacay-cabin"


def _fmt_price(price: int | None) -> str:
    return f"€ {price:,.0f}".replace(",", ".") if price else "Prijs onbekend"


def build_email_html(new_listings: list[dict]) -> str:
    """Bouw een HTML-e-mail: grote knop bovenaan, woningen in grid van 2 per rij."""
    sorted_listings = sorted(new_listings, key=lambda l: l.get("price") or 0, reverse=True)
    count  = len(sorted_listings)
    plural = "en" if count > 1 else ""
    ts     = datetime.now().strftime("%d %B %Y om %H:%M")

    # Kaartjes in rijen van 2
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
                '<div style="width:100%;height:80px;background:#e9ecef;'
                'border-radius:8px 8px 0 0;text-align:center;font-size:2rem;'
                'line-height:80px;">🏡</div>'
            )

            cells += f"""
              <td width="50%" valign="top" style="padding:6px;">
                <div style="background:#ffffff;border-radius:10px;
                            box-shadow:0 2px 8px rgba(0,0,0,.12);overflow:hidden;">
                  {img_html}
                  <div style="padding:12px;">
                    <div style="font-size:10px;font-weight:700;color:#2d6a4f;
                                text-transform:uppercase;letter-spacing:.04em;
                                margin-bottom:4px;">{source}</div>
                    <div style="font-size:13px;font-weight:700;color:#212529;
                                margin-bottom:6px;line-height:1.35;">{title}</div>
                    <div style="font-size:17px;font-weight:800;color:#2d6a4f;
                                margin-bottom:8px;">{price}</div>
                    <div style="font-size:12px;color:#343a40;margin-bottom:2px;">
                      📍 {location}</div>
                    <div style="font-size:12px;color:#343a40;margin-bottom:2px;">
                      🛏 {bedrooms} slaapkamers</div>
                    <div style="font-size:12px;color:#343a40;margin-bottom:10px;">
                      👥 {persons} personen</div>
                    <a href="{WEBSITE_URL}"
                       style="display:block;background:#2d6a4f;color:#ffffff;
                              text-decoration:none;padding:8px 4px;border-radius:6px;
                              font-size:12px;font-weight:600;text-align:center;">
                      Bekijk op website →</a>
                  </div>
                </div>
              </td>"""

        if len(pair) == 1:
            cells += '<td width="50%">&nbsp;</td>'

        rows_html += f"<tr>{cells}</tr>"

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f8f9fa;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:640px;margin:2rem auto;padding:0 1rem;">

    <!-- Header + grote knop -->
    <div style="background:linear-gradient(135deg,#2d6a4f,#40916c);
                border-radius:12px 12px 0 0;padding:2rem 1.5rem 2rem;text-align:center;">
      <h1 style="margin:0 0 .5rem;font-size:1.45rem;color:#ffffff;">
        🏡 {count} Nieuwe vakantiewoning{plural} te koop
      </h1>
      <p style="margin:0 0 1.5rem;font-size:.875rem;color:#d8f3dc;">
        Gevonden op {ts}
      </p>
      <a href="{WEBSITE_URL}"
         style="display:inline-block;background:#f4a261;color:#ffffff;
                text-decoration:none;padding:14px 32px;border-radius:50px;
                font-size:1.05rem;font-weight:700;letter-spacing:.02em;">
        Bekijk {count} nieuwe woningen →
      </a>
    </div>

    <!-- 2-koloms kaartjes grid -->
    <div style="background:#f8f9fa;padding:16px 0;">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;">
        {rows_html}
      </table>
    </div>

    <!-- Footer -->
    <div style="background:#e9ecef;border-radius:0 0 12px 12px;
                padding:1rem 1.5rem;font-size:.75rem;color:#495057;text-align:center;">
      Project Vacay Cabin · max €{config.MAX_PRICE:,} ·
      min {config.MIN_BEDROOMS} slaapkamers · min {config.MIN_PERSONS} personen<br>
      <a href="{WEBSITE_URL}" style="color:#2d6a4f;text-decoration:none;">
        {WEBSITE_URL}</a>
    </div>

  </div>
</body>
</html>"""


def send_email(new_listings: list[dict]) -> None:
    if not config.EMAIL_TO:
        log.warning("E-mail: geen ontvangers ingesteld (EMAIL_TO is leeg)")
        return
    if not config.SMTP_PASSWORD or "JOUW" in config.SMTP_PASSWORD:
        log.warning("E-mail: SMTP_PASSWORD niet ingesteld – sla e-mail over")
        return

    count    = len(new_listings)
    plural   = "en" if count > 1 else ""
    date_str = datetime.now().strftime("%d %B %Y")
    subject  = (
        f"Project Vacay Cabin - {count} nieuwe woning{plural} gevonden - {date_str}"
    )

    html_body = build_email_html(new_listings)
    # Plat-text fallback
    lines = [f"Nieuwe vakantiewoningen te koop ({count}):\n"]
    for l in sorted(new_listings, key=lambda x: x.get("price") or 0, reverse=True):
        lines.append(
            f"- {l['title']} | {_fmt_price(l.get('price'))} | "
            f"{l.get('bedrooms','?')} slpk | {l.get('persons','?')} pers | {l.get('url','')}"
        )
    text_body = "\n".join(lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_FROM
    msg["To"]      = ", ".join(config.EMAIL_TO)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_bytes())
        log.info("E-mail verstuurd naar: %s", ", ".join(config.EMAIL_TO))
    except Exception as exc:
        log.error("E-mail versturen mislukt: %s", exc)


# ════════════════════════════════════════════════════════════
#  Hoofdlogica
# ════════════════════════════════════════════════════════════

def run() -> None:
    log.info("=" * 60)
    log.info("Vakantiewoning scraper gestart")
    log.info("Filters: max €%s · min %d slaapkamers · min %d personen",
             f"{config.MAX_PRICE:,}", config.MIN_BEDROOMS, config.MIN_PERSONS)
    log.info("=" * 60)

    known = load_known()
    log.info("Bekende woningen (known_listings.json): %d", len(known))

    # ── Stap 1: scrapers ─────────────────────────────────────
    all_raw: list[dict] = []
    for scraper in [
        scrape_recreatievastgoed,
        scrape_marktplaats,
        scrape_vakantiehuistekoop,
        scrape_landalmakelaardij,
        scrape_roompot,
    ]:
        try:
            all_raw.extend(scraper())
        except Exception as exc:
            log.error("Scraper %s crashte: %s", scraper.__name__, exc)

    log.info("Totaal gescraped (ongefilterd): %d", len(all_raw))

    # ── Bijhouden welke bronnen actief waren en welke URLs gezien ──
    # Dit gebruiken we straks om offline-status te bepalen.
    scraped_url_keys: set[str] = set()
    active_sources:   set[str] = set()
    for l in all_raw:
        k = url_key(l.get("url", ""))
        if k:
            scraped_url_keys.add(k)
        if l.get("source"):
            active_sources.add(l["source"])

    # ── Stap 2: grove filters (regio + prijs + personen) ─────
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

    # ── Stap 3: slaapkamers ophalen van detailpagina ─────────
    without_beds = [l for l in pre if l.get("bedrooms") is None]
    if without_beds:
        log.info("Slaapkamers ophalen voor %d woningen…", len(without_beds))
        pre = enrich_with_details(pre)

    # ── Stap 4: slaapkamer filter ────────────────────────────
    filtered = [l for l in pre if passes_filters(l)]
    log.info("Na volledige filter: %d woningen", len(filtered))

    # ── Stap 5: deduplicatie op URL (waterdicht) ──────────────
    # Gebruik url_key() zodat trailing slashes, query-strings en
    # fragmenten nooit voor dubbele meldingen zorgen.
    new_listings: list[dict] = []
    updated_known = dict(known)

    for l in filtered:
        key = url_key(l.get("url", ""))
        if not key:
            key = f"{l['source']}::{l['title']}"
        l_stored = {**l, "offline": False}   # vers gevonden → zeker online
        if key not in updated_known:
            new_listings.append(l)
            log.info(
                "NIEUW ★  %-22s | %-40s | %s slpk | %s pers | %s",
                l["source"], l["title"][:40],
                l.get("bedrooms") or "?",
                l.get("persons") or "?",
                _fmt_price(l.get("price")),
            )
        updated_known[key] = l_stored        # altijd bijwerken met verse data

    log.info("Nieuwe woningen deze run: %d", len(new_listings))

    # ── Stap 5b: offline-status bijwerken ────────────────────
    # Woningen van een actieve bron die NIET meer in de scrape zitten
    # → gemarkeerd als offline. Ze blijven wel in de database.
    offline_count = 0
    for key, listing in updated_known.items():
        src = listing.get("source", "")
        if src in active_sources:
            was_seen = key in scraped_url_keys
            if not was_seen and not listing.get("offline"):
                log.info("OFFLINE  %-22s | %s", src, listing.get("title", "")[:50])
                offline_count += 1
            listing["offline"] = not was_seen
    if offline_count:
        log.info("%d woningen gemarkeerd als offline", offline_count)

    # ── Stap 6: opslaan ──────────────────────────────────────
    save_known(updated_known)

    # ── Stap 7: data.json voor website bijwerken ─────────────
    # all_listings = ALLE ooit gevonden woningen (ook offline/verkocht)
    # zodat ze nooit van de website verdwijnen.
    all_for_website = list(updated_known.values())
    write_data_json(
        all_listings=all_for_website,
        new_listings=new_listings,
    )

    # ── Stap 8: e-mail sturen ────────────────────────────────
    if new_listings:
        send_email(new_listings)
    else:
        log.info("Geen nieuwe woningen – geen e-mail verstuurd.")

    log.info("Klaar.")


if __name__ == "__main__":
    run()
