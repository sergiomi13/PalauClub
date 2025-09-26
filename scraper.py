import hashlib
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import tz
import dateparser
from ics import Calendar, Event
from ics.grammar.parse import ContentLine  # << añadir

from playwright.sync_api import sync_playwright

AGENDA_URL = "https://palausantjordi.barcelona/es/agenda"
DEFAULT_TZ = tz.gettz("Europe/Madrid")
DEFAULT_TIME = (20, 0)  # 20:00 si no hay hora
OUTPUT_DIR = os.path.join("public")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "events.ics")
CAL_NAME = "Agenda Palau Sant Jordi"

DATE_REGEX = re.compile(r"(\d{1,2})\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ]+)\s+de\s+(\d{4})", re.IGNORECASE)
TIME_REGEX = re.compile(r"(\d{1,2}):(\d{2})")

MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12
}


def get_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ))
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)
        html = page.content()
        browser.close()
    return html


def parse_events_from_page(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("article, .views-row, .event, .node--type-event, .card")
    events = []
    for card in cards:
        title_tag = card.select_one("h2 a, h3 a, a[href*='/es/'], a[href*='/en/'], a[href*='/ca/']")
        if not title_tag or not title_tag.get_text(strip=True):
            title_tag = card.select_one("h2, h3")
        title = title_tag.get_text(strip=True) if title_tag else None

        href = None
        if title_tag and title_tag.has_attr("href"):
            href = urljoin(base_url, title_tag["href"])

        date_block = None
        for sel in [
            ".date", ".field--name-field-date", ".event-date", ".node__meta", ".info", ".meta", "time"
        ]:
            candidate = card.select_one(sel)
            if candidate and candidate.get_text(strip=True):
                date_block = candidate.get_text(" ", strip=True)
                break
        if not date_block:
            date_block = card.get_text(" ", strip=True)

        when_text = date_block

        location = None
        loc_tag = card.select_one(".location, .field--name-field-venue, .event-venue")
        if loc_tag:
            location = loc_tag.get_text(" ", strip=True)
        if not location:
            location = "Palau Sant Jordi, Barcelona"

        desc_tag = card.select_one(".teaser, .summary, .field--name-body, p")
        description = desc_tag.get_text(" ", strip=True)[:500] if desc_tag else None

        start_dt = extract_datetime_es(when_text)
        if not start_dt:
            continue

        events.append({
            "title": title or "Evento sin título",
            "url": href or base_url,
            "start_dt": start_dt,
            "location": location,
            "description": description or "",
        })

    return dedupe_events(events)


def extract_datetime_es(text: str):
    if not text:
        return None
    dt = dateparser.parse(
        text,
        languages=["es"],
        settings={
            "TIMEZONE": "Europe/Madrid",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future"
        },
    )
    if dt:
        return dt
    m = DATE_REGEX.search(text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = MONTHS_ES.get(month_name)
    if not month:
        return None
    tm = TIME_REGEX.search(text)
    hour, minute = (DEFAULT_TIME if not tm else (int(tm.group(1)), int(tm.group(2))))
    import datetime as _dt
    dt_naive = _dt.datetime(year, month, day, hour, minute)
    return dt_naive.replace(tzinfo=DEFAULT_TZ)


def dedupe_events(events: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for ev in events:
        key = (ev.get("url"), ev.get("title"), ev.get("start_dt"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)
    return unique


def make_uid(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest() + "@palausantjordi"


def scrape_all_pages(start_url: str) -> list[dict]:
    html = get_html(start_url)
    events = parse_events_from_page(html, start_url)

    soup = BeautifulSoup(html, "lxml")
    page_links = set()
    for a in soup.select("a[href*='?page='], a[rel='next']"):
        href = urljoin(start_url, a.get("href"))
        page_links.add(href)

    visited = set([start_url])
    queue = [u for u in sorted(page_links) if u not in visited]
    while queue and len(visited) < 10:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        html_i = get_html(url)
        events.extend(parse_events_from_page(html_i, url))

        soup_i = BeautifulSoup(html_i, "lxml")
        for a in soup_i.select("a[href*='?page='], a[rel='next']"):
            href = urljoin(start_url, a.get("href"))
            if href not in visited and href not in queue and len(visited) + len(queue) < 10:
                queue.append(href)

    return dedupe_events(events)


def build_ics(events: list[dict]) -> Calendar:
    cal = Calendar()
    cal.events = set()
    for ev in events:
        e = Event()
        e.name = ev["title"]
        e.begin = ev["start_dt"]
        e.location = ev.get("location")
        e.url = ev.get("url")
        e.description = ev.get("description")
        e.uid = make_uid(ev.get("url") or ev.get("title"))
        cal.events.add(e)
    # Línea corregida: usar ContentLine en vez de tupla
    cal.extra.append(ContentLine(name="X-WR-CALNAME", params={}, value=CAL_NAME))
    return cal


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    events = scrape_all_pages(AGENDA_URL)
    cal = build_ics(events)
    with

