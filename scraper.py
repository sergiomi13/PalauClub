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
        page = context.ne
