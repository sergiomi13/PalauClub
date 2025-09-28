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
MAX_PAGES = 4          # nº máx de páginas de agenda a recorrer
MAX_EVENTS = 120       # tope de eventos por ejecución

# Regex útiles para fallback de fecha/hora
DATE_REGEX = re.compile(r"(\d{1,2})\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ]+)\s+de\s+(\d{4})", re.IGNORECASE)
TIME_REGEX = re.compile(r"(\d{1,2}):(\d{2})")
MONTHS_ES = {
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
    "julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,
    "noviembre":11,"diciembre":12
}

# -----------------------
# Infra Playwright rápida
# -----------------------
class Browser:
    """Navegador Playwright reutilizable; bloquea recursos pesados y espera al render."""
    def __enter__(self):
        self._p = sync_playwright().start()
        self.browser = self._p.chromium.launch(headless=True)
        self.context = self.browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ))
        # Bloquear imágenes, CSS, fuentes, media
        def _route(route):
            if route.request.resource_type in ("image", "stylesheet", "font", "media"):
                return route.abort()
            return route.continue_()
        self.context.route("**/*", _route)
        self.page = self.context.new_page()
        self.page.set_default_navigation_timeout(60_000)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.context.close(); self.browser.close(); self._p.stop()
        except Exception:
            pass

    def get_html(self, url: str) -> str:
        # Espera a fin de peticiones y a tener tarjetas en DOM
        self.page.goto(url, wait_until="networkidle")
        try:
            self.page.wait_for_selector("article, .views-row, .event, .node--type-event", timeout=60000)
        except Exception:
            print(f"⚠️ No se encontraron tarjetas en {url}")
        return self.page.content()

# -----------------------
# Utilidades de parseo
# -----------------------
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
    return dt_naive.replace(tzinfo=DEFAULT_TZ)

def absolutize(base_url: str, href: str | None) -> str | None:
    if not href: return None
    return urljoin(base_url, href)

def links_from_listing(html: str, base_url: str) -> list[str]:
    """Enlaces candidatos a fichas desde la página de agenda (estrategia laxa y dedup)."""
    soup = BeautifulSoup(html, "lxml")
    raw = []
    # Links dentro de tarjetas (más probables)
    for card in soup.select("article, .views-row, .event, .node--type-event, .card, .node, .teaser"):
        for a in card.select("a[href]"):
            raw.append(a["href"])
    # Y por si acaso, cualquier <a>
    for a in soup.select("a[href]"):
        raw.append(a["href"])

    out, seen = [], set()
    for href in raw:
        href_abs = absolutize(base_url, href)
        if not href_abs or href_abs in seen:
            continue
        p = urlparse(href_abs)
        # mismo dominio
        if "palausantjordi.barcelona" not in (p.netloc or ""):
            continue
        # fuera paginación/listado/anchors
        if "page=" in (p.query or ""): continue
        if p.fragment: continue
        if p.path.rstrip("/") == "/es/agenda": continue
        # algo de profundidad en ruta
        if p.path.count("/") < 3: continue
        seen.add(href_abs); out.append(href_abs)
    return out

def event_from_detail(html: str, url: str) -> dict | None:
    """Desde la FICHA: título (H1/Title), fecha/hora y lugar (más robusto)."""
    soup = BeautifulSoup(html, "lxml")

    # Título robusto
    title = None
    for sel in ["h1", ".page-title h1", ".node__title h1", ".title h1", "header h1"]:
        h = soup.select_one(sel)
        if h and h.get_text(strip=True):
            title = h.get_text(strip=True); break
    if not title:
        tt = soup.select_one("title")
        if tt and tt.get_text(strip=True):
            title = tt.get_text(strip=True).strip()

    # Fecha/hora (busca nodos típicos; si no, todo el texto)
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

    # Descripción breve
    desc = ""
    d = soup.select_one(".teaser, .summary, .field--name-body, article p, .content p")
    if d:
        desc = d.get_text(" ", strip=True)[:500]

    # 🟢 DEBUG
    print(f"Evento encontrado: {title or 'Evento'} | {dt} | {venue}")

    return {
        "title": title or "Evento",
        "url": url,
        "start_dt": dt,
        "location": venue,
        "description": desc,
    }

# -----------------------
# Scrapeo principal
# -----------------------
def scrape_all_events() -> list[dict]:
    events = []
    with Browser() as session:
        # 1) agenda principal
        first_html = session.get_html(AGENDA_URL)

        # Guardar debug de la agenda descargada
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, "debug.html"), "w", encoding="utf-8") as f:
            f.write(first_html)
        print("✅ Guardado public/debug.html con la agenda descargada")

        # 2) descubrir paginación sencilla
        soup = BeautifulSoup(first_html, "lxml")
        page_urls = [AGENDA_URL]
        for a in soup.select("a[href*='?page='], a[rel='next']"):
            page_urls.append(urljoin(AGENDA_URL, a.get("href")))
        # limitar páginas y dedupe
        page_urls = list(dict.fromkeys(page_urls))[:MAX_PAGES]

        # 3) recorrer páginas y entrar a cada ficha
        for i, page_url in enumerate(page_urls, start=1):
            html = first_html if page_url == AGENDA_URL else session.get_html(page_url)
            links = links_from_listing(html, page_url)
            print(f"Página {i}: {len(links)} enlaces candidatos")
            for link in links:
                if len(events) >= MAX_EVENTS:
                    break
                try:
                    detail_html = session.get_html(link)
                    ev = event_from_detail(detail_html, link)
                    if ev:
                        events.append(ev)
                except Exception as e:
                    print(f"⚠️ Error leyendo ficha {link}: {e}")
                    continue
            if len(events) >= MAX_EVENTS:
                break

    # dedupe por (url, fecha)
    seen, uniq = set(), []
    for ev in events:
        key = (ev["url"], ev["start_dt"])
        if key in seen:
            continue
        seen.add(key); uniq.append(ev)
    return uniq

# -----------------------
# Generación ICS
# -----------------------
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

# -----------------------
# Main
# -----------------------
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    events = scrape_all_events()
    print(f"🔎 Total eventos detectados: {len(events)}")
    cal = build_ics(events)

    if not cal.events:
        print("⚠️ No se encontraron eventos, no se generará el .ics")
        return

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.writelines(cal)
    print(f"✅ Generado {OUTPUT_PATH} con {len(list(cal.events))} eventos")

if __name__ == "__main__":
    main()
