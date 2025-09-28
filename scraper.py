import hashlib
import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import tz
import dateparser
from ics import Calendar, Event
from ics.grammar.parse import ContentLine
from playwright.sync_api import sync_playwright

AGENDA_URL = "https://palausantjordi.barcelona/es/agenda"
DEFAULT_TZ = tz.gettz("Europe/Madrid")
DEFAULT_TIME = (20, 0)  # 20:00 si no hay hora
OUTPUT_DIR = os.path.join("public")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "events.ics")
CAL_NAME = "Agenda Palau Sant Jordi"

# Rendimiento
MAX_PAGES = 4
MAX_EVENTS = 120

DATE_REGEX = re.compile(r"(\d{1,2})\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ]+)\s+de\s+(\d{4})", re.IGNORECASE)
TIME_REGEX = re.compile(r"(\d{1,2}):(\d{2})")
MONTHS_ES = {
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
    "julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,
    "noviembre":11,"diciembre":12
}

class Browser:
    """Reutiliza un navegador Playwright y bloquea recursos pesados."""
    def __enter__(self):
        self._p = sync_playwright().start()
        self.browser = self._p.chromium.launch(headless=True)
        self.context = self.browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ))
        def _route(route):
            if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                return route.abort()
            return route.continue_()
        self.context.route("**/*", _route)
        self.page = self.context.new_page()
        self.page.set_default_navigation_timeout(30_000)
        return self
    def __exit__(self, exc_type, exc, tb):
        try:
            self.context.close(); self.browser.close(); self._p.stop()
        except Exception:
            pass
    def get_html(self, url: str) -> str:
        self.page.goto(url, wait_until="domcontentloaded")
        return self.page.content()

def extract_datetime_es(text: str):
    if not text:
        return None
    dt = dateparser.parse(
        text, languages=["es"],
        settings={
            "TIMEZONE": "Europe/Madrid",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
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
    return dt_naive.replace(tzinfo=tz.gettz("Europe/Madrid"))

def parse_listing_get_links(html: str, base_url: str) -> list[str]:
    """
    Coge enlaces potenciales a fichas desde la página de agenda.
    Estrategia laxa: cualquier <a> interno que NO sea paginación ni la propia /agenda.
    """
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        href_abs = urljoin(base_url, href)
        p = urlparse(href_abs)
        # Debe ser del mismo dominio
        if "palausantjordi.barcelona" not in (p.netloc or ""):
            continue
        # fuera paginación/listado
        if "page=" in (p.query or ""):
            continue
        if p.path.rstrip("/") == "/es/agenda":
            continue
        # descartamos anchors
        if p.fragment:
            continue
        # un poco de profundidad en la ruta
        if p.path.count("/") < 3:
            continue
        links.append(href_abs)

    # dedupe conservando orden
    seen, out = set(), []
    for h in links:
        if h not in seen:
            seen.add(h); out.append(h)
    return out

def parse_event_detail(html: str, url: str) -> dict | None:
    """Desde la FICHA: título (H1/Title), fecha/hora y lugar."""
    soup = BeautifulSoup(html, "lxml")
    # Título
    title = None
    for sel in ["h1", ".page-title h1", ".node__title h1", ".title h1", "header h1"]:
        h = soup.select_one(sel)
        if h and h.get_text(strip=True):
            title = h.get_text(strip=True); break
    if not title:
        tt = soup.select_one("title")
        if tt and tt.get_text(strip=True):
            title = tt.get_text(strip=True).strip()

    # Fecha/hora
    date_text = None
    for sel in [".date", ".field--name-field-date", ".event-date", ".node__meta", ".info", ".meta", "time"]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            date_text = n.get_text(" ", strip=True); break
    if not date_text:
        date_text = soup.get_text(" ", strip=True)[:3000]
    dt = extract_datetime_es(date_text)
    if not dt:
        return None

    # Lugar
    venue = "Palau Sant Jordi, Barcelona"
    for sel in [".location", ".field--name-field-venue", ".event-venue", "[itemprop='location']"]:
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            venue = n.get_text(" ", strip=True); break

    # Descripción corta
    desc = ""
    d = soup.select_one(".teaser, .summary, .field--name-body, article p, .content p")
    if d:
        desc = d.get_text(" ", strip=True)[:500]

    return {
        "title": title or "Evento",
        "url": url,
        "start_dt": dt,
        "location": venue,
        "description": desc,
    }

def scrape_all_events() -> list[dict]:
    events = []
    with Browser() as session:
        # 1) agenda principal
        first_html = session.get_html(AGENDA_URL)

        # 🔎 GUARDAR DEBUG de la agenda para inspección
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, "debug.html"), "w", encoding="utf-8") as f:
            f.write(first_html)
        print("✅ Guardado public/debug.html con la agenda descargada")

        # 2) paginación simple
        soup = BeautifulSoup(first_html, "lxml")
        page_urls = [AGENDA_URL]
        for a in soup.select("a[href*='?page='], a[rel='next']"):
            page_urls.append(urljoin(AGENDA_URL, a.get("href")))
        # limitar páginas
        page_urls = list(dict.fromkeys(page_urls))[:MAX_PAGES]

        # 3) recorrer páginas y fichas
        for i, page_url in enumerate(page_urls):
            html = first_html if i == 0 else session.get_html(page_url)
            links = parse_listing_get_links(html, page_url)
            print(f"Página {i+1}: {len(links)} enlaces candidatos")
            for link in links:
                if len(events) >= MAX_EVENTS:
                    break
                try:
                    detail_html = session.get_html(link)
                    ev = parse_event_detail(detail_html, link)
                    if ev:
                        events.append(ev)
                except Exception:
                    continue
            if len(events) >= MAX_EVENTS:
                break

    # dedupe por url+fecha
    seen, uniq = set(), []
    for ev in events:
        key = (ev["url"], ev["start_dt"])
        if key in seen:
            continue
        seen.add(key); uniq.append(ev)
    return uniq

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
        e.uid = hashlib.sha1((ev.get("url") or ev["title"]).encode("utf-8")).hexdigest() + "@palausantjordi"
        cal.events.add(e)
    cal.extra.append(ContentLine(name="X-WR-CALNAME", params={}, value=CAL_NAME))
    return cal

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    events = scrape_all_events()
    cal = build_ics(events)
    print(f"Se han detectado {len(list(cal.events))} eventos")
    if not cal.events:
        print("⚠️ No se encontraron eventos, no se generará el .ics")
        # Aun así dejamos debug.html publicado
        return
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.writelines(cal)
    print(f"✅ Generado {OUTPUT_PATH} con {len(list(cal.events))} eventos")

if __name__ == "__main__":
    main()
