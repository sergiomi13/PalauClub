import hashlib
import os
import re
from urllib.parse import urljoin

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

# Páginas a recorrer del listado
MAX_PAGES = 4

# Regex útiles para fallback de fecha/hora
DATE_REGEX = re.compile(r"(\d{1,2})\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ]+)\s+de\s+(\d{4})", re.IGNORECASE)
TIME_REGEX = re.compile(r"(\d{1,2}):(\d{2})")
MONTHS_ES = {
    "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
    "julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,
    "noviembre":11,"diciembre":12
}

# Palabras a ignorar cuando buscamos el título en las tarjetas
NOISE_PREFIXES = {"+ info", "+info", "comprar tickets", "entradas", "gratut", "gratis", "image", "ticket", "agotadas"}
VENUE_WORDS = {"palau sant jordi", "sant jordi club", "estadi olímpic", "estadi olimpic", "olímpic", "olimpic"}

# -----------------------
# Playwright
# -----------------------
class Browser:
    def __enter__(self):
        self._p = sync_playwright().start()
        self.browser = self._p.chromium.launch(headless=True)
        self.context = self.browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ))
        # Bloquear imágenes, CSS, fuentes, media (más rápido)
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
        self.page.goto(url, wait_until="networkidle")
        # esperar a que el listado esté renderizado
        try:
            self.page.wait_for_selector("article, .views-row, .event, .node--type-event, .card, .node, .teaser", timeout=60000)
        except Exception:
            print(f"⚠️ No se encontraron tarjetas en {url}")
        return self.page.content()

# -----------------------
# Utilidades
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

def clean_line(s: str) -> str:
    s = s.strip()
    # normaliza espacios
    s = re.sub(r"\s+", " ", s)
    return s

def looks_like_noise(s: str) -> bool:
    t = s.strip().lower()
    if not t:
        return True
    if any(t.startswith(p) for p in NOISE_PREFIXES):
        return True
    if any(w in t for w in ("aceptar todas", "personalizar", "rechazar", "uso de cookies")):
        return True
    return False

def looks_like_venue(s: str) -> bool:
    t = s.strip().lower()
    return any(v in t for v in VENUE_WORDS)

def first_title_like_line(lines: list[str]) -> str | None:
    for line in lines:
        c = clean_line(line)
        if looks_like_noise(c):
            continue
        # evita que coja el recinto como título
        if looks_like_venue(c):
            continue
        # evita líneas que son solo fecha/hora
        if DATE_REGEX.search(c):
            continue
        if TIME_REGEX.search(c) and len(c) <= 8:
            continue
        # algo con letras
        if re.search(r"[A-Za-zÁÉÍÓÚáéíóúÜüÑñ]", c):
            return c
    return None

# -----------------------
# Scraper (desde listado)
# -----------------------
def parse_list_page_to_events(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("article, .views-row, .event, .node--type-event, .card, .node, .teaser")
    events = []
    for card in cards:
        # recojo todas las líneas de texto visibles de la tarjeta
        lines = [t for t in card.stripped_strings]
        if not lines:
            continue
        # título heurístico
        title = first_title_like_line(lines) or "Evento"
        # fecha/hora del bloque completo
        block_text = clean_line(" ".join(lines))
        start_dt = extract_datetime_es(block_text)
        if not start_dt:
            continue
        # venue por heurística
        venue = None
        for line in lines:
            if looks_like_venue(line):
                venue = clean_line(line); break
        if not venue:
            venue = "Palau Sant Jordi, Barcelona"

        # url: mejor algún enlace válido si existe (aunque sea “+ info”)
        href = None
        for a in card.select("a[href]"):
            href = urljoin(base_url, a.get("href"))
            break

        events.append({
            "title": title,
            "url": href or base_url,
            "start_dt": start_dt,
            "location": venue,
            "description": block_text[:500],
        })

        # DEBUG
        print(f"Evento encontrado: {title} | {start_dt} | {venue}")
    return events

def scrape_all_events() -> list[dict]:
    all_events = []
    with Browser() as session:
        # página 1
        first_html = session.get_html(AGENDA_URL)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, "debug.html"), "w", encoding="utf-8") as f:
            f.write(first_html)
        print("✅ Guardado public/debug.html con la agenda descargada")

        # páginas siguientes (?page=, rel=next)
        soups = [BeautifulSoup(first_html, "lxml")]
        page_urls = [AGENDA_URL]
        for a in soups[0].select("a[href*='?page='], a[rel='next']"):
            page_urls.append(urljoin(AGENDA_URL, a.get("href")))
        page_urls = list(dict.fromkeys(page_urls))[:MAX_PAGES]

        for i, url in enumerate(page_urls, start=1):
            html = first_html if i == 1 else session.get_html(url)
            evs = parse_list_page_to_events(html, url)
            print(f"Página {i}: {len(evs)} eventos")
            all_events.extend(evs)

    # dedupe por (título + fecha)
    seen, uniq = set(), []
    for ev in all_events:
        key = (ev["title"], ev["start_dt"])
        if key in seen: 
            continue
        seen.add(key); uniq.append(ev)
    return uniq

# -----------------------
# ICS
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
        uid_src = ev.get("url") or (ev["title"] + str(ev["start_dt"]))
        e.uid = hashlib.sha1(uid_src.encode("utf-8")).hexdigest() + "@palausantjordi"
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

    if not events:
        print("⚠️ No se encontraron eventos; no se generará el .ics")
        return

    cal = build_ics(events)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.writelines(cal)
    print(f"✅ Generado {OUTPUT_PATH} con {len(list(cal.events))} eventos")

if __name__ == "__main__":
    main()
