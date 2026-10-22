"""
Fonte di risoluzione: gli URL citati nei criteri, nelle note e nello sfondo
della domanda vengono scaricati e il loro testo (pulito e datato) finisce nel
briefing come "what the resolution source says NOW".

Interruttore: FONTE_RISOLUZIONE=no la spegne. Timeout per pagina, al massimo
2 pagine e 6.000 caratteri in tutto. Nessun costo: niente modelli.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from bot.fonti import fred

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_URL = 2
MAX_CARATTERI = 6000
MAX_SCARICATO = 1_500_000  # byte letti al massimo da una pagina
ESTENSIONI_SALTATE = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".xls", ".xlsx", ".doc", ".docx", ".mp4")
DOMINI_SALTATI = ("metaculus.com", "twitter.com", "x.com", "facebook.com", "youtube.com", "instagram.com", "t.me")
INTESTAZIONI = {"User-Agent": "Mozilla/5.0 (compatible; bot-previsioni/2.0; forecasting research)", "Accept-Language": "en"}

# Le parentesi tonde restano nell'URL (Wikipedia: "Electoral_College_(United_States)"); si bilanciano dopo.
_URL = re.compile(r"https?://[^\s<>\"'\]\}]+", flags=re.IGNORECASE)
_ESCAPE_MARKDOWN = re.compile(r"\\([^A-Za-z0-9\s])")  # i testi Metaculus sono markdown: "\(", "\_", "\*"...
_PUNTEGGIATURA_FINALE = ".,;:!?»”\"'"
_META_DATA = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:article:published_time|article:modified_time|datePublished|dateModified|"
    r"og:updated_time|date|last-modified|pubdate)[\"'][^>]+content=[\"']([^\"']+)[\"']",
    flags=re.IGNORECASE,
)
_DATA_ISO = re.compile(r"\b(20[0-9]{2}-[01][0-9]-[0-3][0-9])\b")


def rifinisci_url(grezzo: str) -> str:
    """Toglie gli escape markdown, la punteggiatura finale e le parentesi chiuse non bilanciate."""
    url = _ESCAPE_MARKDOWN.sub(r"\1", grezzo).replace("&amp;", "&")
    while url:
        if url[-1] in _PUNTEGGIATURA_FINALE:
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        else:
            break
    return url


def estrai_url(*testi: str | None) -> list[str]:
    """URL unici (in ordine di apparizione) dai testi, senza i domini inutili e i file binari."""
    trovati: list[str] = []
    for testo in testi:
        if not testo:
            continue
        for grezzo in _URL.findall(testo):
            url = rifinisci_url(grezzo)
            basso = url.lower()
            if any(d in basso for d in DOMINI_SALTATI) or basso.split("?")[0].endswith(ESTENSIONI_SALTATE):
                continue
            if url not in trovati:
                trovati.append(url)
    return trovati


def pulisci_html(contenuto: str) -> str:
    """Testo leggibile da una pagina HTML, senza dipendenze: via script, stili, tag; spazi compattati."""
    testo = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|aside)[^>]*>.*?</\1>", " ", contenuto)
    testo = re.sub(r"(?s)<!--.*?-->", " ", testo)
    testo = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|table|section|article)>", "\n", testo)
    testo = re.sub(r"<[^>]+>", " ", testo)
    testo = html.unescape(testo).replace("\xa0", " ")
    testo = re.sub(r"[ \t\r\f\v]+", " ", testo)
    testo = re.sub(r"\n\s*\n+", "\n", testo)
    return testo.strip()


def _data_pagina(contenuto: str, intestazioni: dict) -> str | None:
    m = _META_DATA.search(contenuto)
    if m:
        return m.group(1)[:25]
    ultima = intestazioni.get("Last-Modified") or intestazioni.get("last-modified")
    if ultima:
        try:
            return parsedate_to_datetime(ultima).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass
    return None


def scarica_fred(url: str, timeout: int = TIMEOUT) -> tuple[str, str]:
    """Le pagine FRED sono vuote senza JavaScript: si legge il CSV pubblico della serie (ultime 12 osservazioni)."""
    serie = fred.id_da_url(url)
    osservazioni = fred.scarica_csv(serie, timeout)
    return fred.descrivi_ultime(serie, osservazioni), osservazioni[-1][0].isoformat()


def scarica(url: str, timeout: int = TIMEOUT) -> tuple[str, str | None]:
    """(testo pulito, data della pagina o None). Solleva se la pagina non si legge."""
    if fred.id_da_url(url):
        return scarica_fred(url, timeout)
    with requests.get(url, headers=INTESTAZIONI, timeout=timeout, stream=True, allow_redirects=True) as risposta:
        risposta.raise_for_status()
        tipo = (risposta.headers.get("Content-Type") or "").lower()
        if not any(t in tipo for t in ("text/", "json", "xml")) and tipo:
            raise ValueError(f"tipo di contenuto non testuale: {tipo}")
        grezzo = b""
        for pezzo in risposta.iter_content(65536):
            grezzo += pezzo
            if len(grezzo) >= MAX_SCARICATO:
                break
        codifica = risposta.encoding or "utf-8"
        contenuto = grezzo.decode(codifica, errors="replace")
        data = _data_pagina(contenuto, dict(risposta.headers))
    testo = pulisci_html(contenuto) if "html" in tipo or "<html" in contenuto[:2000].lower() else contenuto.strip()
    if not data:
        m = _DATA_ISO.search(testo[:3000])
        data = m.group(1) if m else None
    if len(testo) < 80:
        raise ValueError("pagina quasi vuota")
    return testo, data


async def fonte_risoluzione(domanda, scarica_pagina=scarica, max_caratteri: int = MAX_CARATTERI) -> str | None:
    """Sezione del briefing con le pagine citate dai criteri, o None se non ce ne sono di leggibili."""
    url = estrai_url(domanda.resolution_criteria, domanda.fine_print, domanda.background_info)[:MAX_URL]
    if not url:
        logger.info("Fonte di risoluzione: nessun URL leggibile nei criteri")
        return None
    oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    quota = max_caratteri // len(url)
    parti: list[str] = []
    for indirizzo in url:
        try:
            testo, data = await asyncio.wait_for(asyncio.to_thread(scarica_pagina, indirizzo), timeout=TIMEOUT + 5)
        except Exception as errore:
            logger.warning(f"Fonte di risoluzione {indirizzo} non letta: {type(errore).__name__}: {errore}")
            parti.append(f"### {indirizzo}\n(could not be fetched today: {type(errore).__name__})")
            continue
        estratto = testo[:quota]
        if len(testo) > quota:
            estratto += " [...]"
        parti.append(f"### {indirizzo}\nPage date: {data or 'not stated'}. Fetched: {oggi}.\n{estratto}")
    letti = sum(1 for p in parti if "Fetched:" in p)
    logger.info(f"Fonte di risoluzione: {letti}/{len(url)} pagine lette ({', '.join(url)})")
    if not letti:
        return None
    return (
        "What the resolution source says NOW (fetched today; check the page date; this is the text the "
        "question will most likely be resolved against):\n" + "\n\n".join(parti)
    )
