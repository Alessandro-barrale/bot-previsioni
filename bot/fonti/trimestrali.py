"""
Risultati trimestrali delle aziende quotate, per le domande Market Pulse (ricavi,
utili per azione, guidance). Solo fonti gratuite e senza chiave:

- SEC EDGAR, API XBRL ufficiale (gratuita, senza chiave; chiede solo uno
  User-Agent con un contatto, impostabile con SEC_USER_AGENT): ricavi, EPS
  diluito e utile netto degli ultimi trimestri dai 10-Q e 10-K, con il quarto
  trimestre ricavato dall'anno meno i primi tre (segnato come "derived");
- Yahoo Finance quoteSummary (con il "crumb", come fa yfinance): consenso degli
  analisti per il trimestre in corso e il prossimo (ricavi ed EPS: media,
  minimo, massimo, numero di analisti), sorprese sull'EPS degli ultimi
  trimestri, data dei prossimi risultati. Endpoint non ufficiale: può cadere;
- Yahoo Finance chart (lo stesso di serie_storiche): ultimo prezzo e variazione
  a 3 mesi, solo come contesto.

Ogni pezzo può fallire senza bloccare gli altri. Quello che le fonti gratuite non
danno (la storia delle sorprese sui ricavi, la guidance) lo cerca la ricerca web,
guidata da bot.market_pulse.GUIDA_WEB. Costo: zero, più al massimo una chiamata
al modello economico quando l'azienda non è nell'elenco qui sotto.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from bot import combina
from bot.fonti.serie_storiche import INTESTAZIONI, scarica_yahoo

logger = logging.getLogger(__name__)

TIMEOUT = 20
# La SEC vuole uno User-Agent "nome contatto": rifiuta (403) il dominio noreply di GitHub.
# Il predefinito usa example.com (dominio riservato, di nessuno); SEC_USER_AGENT lo sostituisce.
UA_SEC_PREDEFINITO = "bot-previsioni research-bot@example.com"

# Nomi (minuscoli, parole intere) -> ticker. I nomi più lunghi vanno prima dei più corti.
AZIENDE: dict[str, str] = {
    "advanced micro devices": "AMD",
    "taiwan semiconductor": "TSM",
    "meta platforms": "META",
    "arm holdings": "ARM",
    "nvidia": "NVDA",
    "apple": "AAPL",
    "amd": "AMD",
    "amazon": "AMZN",
    "aws": "AMZN",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "microsoft": "MSFT",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "netflix": "NFLX",
    "broadcom": "AVGO",
    "oracle": "ORCL",
    "intel": "INTC",
    "tsmc": "TSM",
    "palantir": "PLTR",
    "salesforce": "CRM",
    "micron": "MU",
    "qualcomm": "QCOM",
    "adobe": "ADBE",
    "coreweave": "CRWV",
    "walmart": "WMT",
    "costco": "COST",
    "jpmorgan": "JPM",
    "uber": "UBER",
    "shopify": "SHOP",
    "coinbase": "COIN",
    "spotify": "SPOT",
    "disney": "DIS",
    "nike": "NKE",
    "boeing": "BA",
    "ibm": "IBM",
    "cisco": "CSCO",
}

CONCETTI_RICAVI = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
_CACHE_CIK: dict[str, int] = {}


# ------------------------------------------------------------ azienda e ticker
def _testi_domanda(domanda) -> list[str]:
    """Dove cercare il nome dell'azienda, dal più specifico: etichetta della sotto-domanda, testo, titolo del gruppo."""
    api = getattr(domanda, "api_json", None) or {}
    testi = [getattr(domanda, "group_question_option", None), domanda.question_text, api.get("title")]
    return [t for t in testi if isinstance(t, str) and t.strip()]


def trova_azienda(domanda) -> tuple[str, str] | None:
    """(nome, ticker) dell'azienda della domanda dall'elenco AZIENDE, o None."""
    for testo in _testi_domanda(domanda):
        minuscolo = testo.lower()
        trovate = []
        for nome, ticker in AZIENDE.items():
            m = re.search(rf"\b{re.escape(nome)}\b", minuscolo)
            if m:
                trovate.append((m.start(), -len(nome), nome, ticker))
        if trovate:
            _, _, nome, ticker = min(trovate)
            return nome, ticker
    return None


def _prompt_azienda(domanda) -> str:
    return (
        "TRIMESTRALI_AZIENDA\n"
        "Which publicly listed company is the question below about? Give the company name and its Yahoo Finance "
        'ticker for the main US listing (for example "NVDA"). Use null for the ticker if the company is not '
        "publicly listed or you are not sure the ticker exists.\n\n"
        f"Question: {' — '.join(_testi_domanda(domanda))}\n\n"
        'Answer with one JSON block only: {"company": "...", "ticker": "..." or null}'
    )


async def identifica_azienda(domanda, modello=None) -> tuple[str, str] | None:
    trovata = trova_azienda(domanda)
    if trovata is not None or modello is None:
        return trovata
    oggetto = combina.leggi_json_finale(await modello.invoke(_prompt_azienda(domanda))) or {}
    ticker = oggetto.get("ticker")
    if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z0-9.\-^=]{1,12}", ticker.strip()):
        return None
    return str(oggetto.get("company") or ticker).strip(), ticker.strip().upper()


# ------------------------------------------------------------ SEC EDGAR
def _intestazioni_sec() -> dict:
    return {"User-Agent": os.getenv("SEC_USER_AGENT", "").strip() or UA_SEC_PREDEFINITO, "Accept": "application/json"}


def cik_da_ticker(ticker: str) -> int:
    chiave = ticker.upper().replace("-", ".")
    if not _CACHE_CIK:
        risposta = requests.get("https://www.sec.gov/files/company_tickers.json", headers=_intestazioni_sec(), timeout=TIMEOUT)
        risposta.raise_for_status()
        for voce in risposta.json().values():
            _CACHE_CIK[str(voce["ticker"]).upper()] = int(voce["cik_str"])
    if chiave not in _CACHE_CIK:
        raise ValueError(f"ticker non presente alla SEC: {ticker}")
    return _CACHE_CIK[chiave]


def scarica_concetto(cik: int, concetto: str) -> dict:
    risposta = requests.get(
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{concetto}.json",
        headers=_intestazioni_sec(),
        timeout=TIMEOUT,
    )
    risposta.raise_for_status()
    return risposta.json()


@dataclass
class Trimestre:
    inizio: date
    fine: date
    valore: float
    derivato: bool = False  # quarto trimestre = anno - (primi tre)


def trimestri_da_fatti(fatti: list[dict]) -> list[Trimestre]:
    """
    Trimestri dai fatti XBRL di un concetto (lista "units" -> "USD" della SEC): periodi di
    80-100 giorni dai 10-Q/10-K (a parità di fine vale l'ultimo depositato); il quarto
    trimestre che manca si ricava dall'anno (350-380 giorni) meno i tre trimestri dentro.
    """
    quarti: dict[date, tuple[str, date, float]] = {}
    anni: dict[date, tuple[str, date, float]] = {}
    for fatto in fatti:
        if not str(fatto.get("form", "")).startswith(("10-Q", "10-K")):
            continue
        try:
            inizio = date.fromisoformat(fatto["start"])
            fine = date.fromisoformat(fatto["end"])
            valore = float(fatto["val"])
        except (KeyError, TypeError, ValueError):
            continue
        durata = (fine - inizio).days
        destinazione = quarti if 80 <= durata <= 100 else anni if 350 <= durata <= 380 else None
        if destinazione is None:
            continue
        depositato = str(fatto.get("filed") or "")
        if fine not in destinazione or depositato > destinazione[fine][0]:
            destinazione[fine] = (depositato, inizio, valore)
    trimestri = {fine: Trimestre(inizio, fine, valore) for fine, (_, inizio, valore) in quarti.items()}
    for fine_anno, (_, inizio_anno, valore_anno) in anni.items():
        if any(abs((f - fine_anno).days) <= 5 for f in trimestri):
            continue  # il quarto trimestre c'è già
        dentro = sorted(
            (t for t in trimestri.values() if not t.derivato and t.inizio >= inizio_anno - timedelta(days=5) and t.fine <= fine_anno),
            key=lambda t: t.fine,
        )
        if len(dentro) != 3:
            continue
        inizio_q4 = dentro[-1].fine + timedelta(days=1)
        if not 80 <= (fine_anno - inizio_q4).days <= 100:
            continue
        trimestri[fine_anno] = Trimestre(inizio_q4, fine_anno, valore_anno - sum(t.valore for t in dentro), derivato=True)
    return sorted(trimestri.values(), key=lambda t: t.fine)


def _fatti(concetto_json: dict, unita: str) -> list[dict]:
    return list(((concetto_json.get("units") or {}).get(unita)) or [])


def _soldi(valore: float) -> str:
    assoluto = abs(valore)
    if assoluto >= 1e9:
        return f"{valore / 1e9:,.2f} B"
    if assoluto >= 1e6:
        return f"{valore / 1e6:,.1f} M"
    return f"{valore:,.2f}"


def _variazione(nuovo: float, vecchio: float | None) -> str | None:
    if vecchio is None or vecchio == 0:
        return None
    return f"{(nuovo - vecchio) / abs(vecchio):+.1%}"


def descrivi_trimestri(trimestri: list[Trimestre], nome: str, unita: str, quanti: int = 8, soldi: bool = True) -> list[str]:
    if not trimestri:
        return []
    per_fine = {t.fine: t for t in trimestri}

    def un_anno_prima(t: Trimestre) -> Trimestre | None:
        return next((per_fine[f] for f in per_fine if abs((t.fine - f).days - 365) <= 12), None)

    def consecutivi(a: Trimestre, b: Trimestre) -> bool:
        return 80 <= (b.fine - a.fine).days <= 100  # niente buchi: q/q solo tra trimestri attaccati

    righe = [f"{nome}:"]
    ultimi = trimestri[-quanti:]
    for i, t in enumerate(ultimi):
        indice = trimestri.index(t)
        precedente = trimestri[indice - 1] if indice > 0 and consecutivi(trimestri[indice - 1], t) else None
        anno_prima = un_anno_prima(t)
        valore = _soldi(t.valore) if soldi else f"{t.valore:,.2f}"
        note = [n for n in (
            f"y/y {_variazione(t.valore, anno_prima.valore)}" if anno_prima and _variazione(t.valore, anno_prima.valore) else None,
            f"q/q {_variazione(t.valore, precedente.valore)}" if precedente and _variazione(t.valore, precedente.valore) else None,
            "derived: fiscal year minus Q1-Q3" if t.derivato else None,
        ) if n]
        righe.append(f"- quarter ending {t.fine.isoformat()}: {valore} {unita}" + (f" ({', '.join(note)})" if note else ""))
    if soldi and len(ultimi) >= 4:
        crescite = [
            (b.valore - a.valore) / abs(a.valore) for a, b in zip(ultimi, ultimi[1:]) if a.valore and consecutivi(a, b)
        ]
        if len(crescite) >= 3:
            righe.append(
                f"  Sequential (q/q) growth over these quarters: mean {statistics.mean(crescite):+.1%}, "
                f"standard deviation {statistics.pstdev(crescite):.1%}."
            )
    return righe


def dati_sec(ticker: str, scarica=scarica_concetto, cik_di=cik_da_ticker) -> str | None:
    """Ricavi, EPS diluito e utile netto per trimestre dalla SEC; None se l'azienda non deposita in us-gaap."""
    cik = cik_di(ticker)
    migliori: tuple[str, list[Trimestre]] | None = None
    for concetto in CONCETTI_RICAVI:
        try:
            trimestri = trimestri_da_fatti(_fatti(scarica(cik, concetto), "USD"))
        except requests.HTTPError:
            continue  # concetto non usato da questa azienda (404)
        if trimestri and (migliori is None or (trimestri[-1].fine, len(trimestri)) > (migliori[1][-1].fine, len(migliori[1]))):
            migliori = (concetto, trimestri)
    righe: list[str] = []
    if migliori:
        righe += descrivi_trimestri(migliori[1], f"Revenue (XBRL concept {migliori[0]})", "USD")
    for concetto, nome, unita, quanti, soldi in (
        ("EarningsPerShareDiluted", "Diluted EPS", "USD/shares", 8, False),
        ("NetIncomeLoss", "Net income", "USD", 4, True),
    ):
        try:
            trimestri = trimestri_da_fatti(_fatti(scarica(cik, concetto), unita))
        except requests.HTTPError:
            continue
        righe += descrivi_trimestri(trimestri, nome, "USD" if soldi else "USD per share", quanti, soldi)
    if not righe:
        return None
    return (
        "Reported quarterly results (SEC EDGAR XBRL, from the company's 10-Q/10-K filings; fiscal quarter end dates):\n"
        + "\n".join(righe)
    )


# ------------------------------------------------------------ Yahoo Finance
def scarica_sintesi_yahoo(ticker: str, sessione: requests.Session | None = None) -> dict:
    """quoteSummary di Yahoo con cookie e crumb (come yfinance): consenso, sorprese EPS, calendario, prezzo."""
    sessione = sessione or requests.Session()
    sessione.headers.update(INTESTAZIONI)
    try:
        sessione.get("https://fc.yahoo.com", timeout=TIMEOUT)  # imposta il cookie; la risposta 404 è normale
    except requests.RequestException:
        pass
    crumb = sessione.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=TIMEOUT).text.strip()
    if not crumb or "<" in crumb or " " in crumb or len(crumb) > 40:
        raise ValueError("crumb di Yahoo non ottenuto")
    risposta = sessione.get(
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
        params={"modules": "earningsTrend,earningsHistory,calendarEvents,price", "crumb": crumb},
        timeout=TIMEOUT,
    )
    risposta.raise_for_status()
    risultato = ((risposta.json().get("quoteSummary") or {}).get("result")) or []
    if not risultato:
        raise ValueError(f"quoteSummary vuoto per {ticker}")
    return risultato[0]


def _num(valore) -> float | None:
    """Yahoo scrive {"raw": 1.2, "fmt": "1.20"} oppure il numero; {} quando manca."""
    if isinstance(valore, dict):
        valore = valore.get("raw")
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None
    return float(valore)


def _data(valore) -> str | None:
    if isinstance(valore, dict):
        if valore.get("fmt"):
            return str(valore["fmt"])
        valore = valore.get("raw")
    if isinstance(valore, (int, float)) and not isinstance(valore, bool):
        return datetime.fromtimestamp(valore, tz=timezone.utc).date().isoformat()
    if isinstance(valore, str) and valore:
        return valore[:10]
    return None


def _stima(nome: str, voce: dict, soldi: bool) -> str | None:
    media = _num(voce.get("avg"))
    if media is None:
        return None
    formato = _soldi if soldi else (lambda v: f"{v:.2f}")
    parti = []
    basso, alto, analisti = _num(voce.get("low")), _num(voce.get("high")), _num(voce.get("numberOfAnalysts"))
    if basso is not None and alto is not None:
        parti.append(f"low {formato(basso)}, high {formato(alto)}")
    if analisti:
        parti.append(f"{analisti:.0f} analysts")
    anno_prima = _num(voce.get("yearAgoRevenue") if soldi else voce.get("yearAgoEps"))
    if anno_prima is not None:
        parti.append(f"year-ago {formato(anno_prima)}")
    crescita = _num(voce.get("growth"))
    if crescita is not None:
        parti.append(f"growth {crescita:+.1%}")
    return f"{nome} consensus {formato(media)}" + (f" ({', '.join(parti)})" if parti else "")


def descrivi_sintesi(sintesi: dict) -> list[str]:
    righe: list[str] = []
    nomi = {"0q": "current quarter", "+1q": "next quarter", "0y": "current fiscal year"}
    for voce in ((sintesi.get("earningsTrend") or {}).get("trend")) or []:
        periodo = voce.get("period")
        if periodo not in nomi:
            continue
        parti = [p for p in (
            _stima("revenue", voce.get("revenueEstimate") or {}, soldi=True),
            _stima("EPS", voce.get("earningsEstimate") or {}, soldi=False),
        ) if p]
        tendenza = voce.get("epsTrend") or {}
        adesso, prima = _num(tendenza.get("current")), _num(tendenza.get("90daysAgo"))
        if adesso is not None and prima is not None:
            parti.append(f"EPS consensus 90 days ago {prima:.2f}")
        if parti:
            righe.append(f"- {nomi[periodo]} (period ending {voce.get('endDate') or '?'}): " + "; ".join(parti))
    sorprese = []
    for voce in ((sintesi.get("earningsHistory") or {}).get("history")) or []:
        effettivo, stima = _num(voce.get("epsActual")), _num(voce.get("epsEstimate"))
        if effettivo is None or stima is None:
            continue
        sorpresa = _num(voce.get("surprisePercent"))
        if sorpresa is None and stima:
            sorpresa = (effettivo - stima) / abs(stima)
        sorprese.append((_data(voce.get("quarter")) or "?", effettivo, stima, sorpresa))
    if sorprese:
        righe.append(
            "- EPS actual vs consensus, last quarters: "
            + "; ".join(f"{q}: {a:.2f} vs {s:.2f}" + (f" ({p:+.1%})" if p is not None else "") for q, a, s, p in sorprese)
        )
        valori = [p for *_, p in sorprese if p is not None]
        if len(valori) >= 2:
            righe.append(
                f"  EPS surprise: mean {statistics.mean(valori):+.1%}, standard deviation {statistics.pstdev(valori):.1%}, "
                f"beats {sum(v > 0 for v in valori)} of {len(valori)}."
            )
    calendario = ((sintesi.get("calendarEvents") or {}).get("earnings")) or {}
    date_risultati = [d for d in (_data(v) for v in calendario.get("earningsDate") or []) if d]
    if date_risultati:
        righe.append(f"- Next earnings report date (Yahoo): {' to '.join(dict.fromkeys(date_risultati))}")
    if not any("revenue consensus" in r for r in righe):
        media = _num(calendario.get("revenueAverage"))
        if media is not None:
            righe.append(
                f"- Revenue consensus for the next report (Yahoo calendar): {_soldi(media)}"
                + (f" (low {_soldi(_num(calendario['revenueLow']))}, high {_soldi(_num(calendario['revenueHigh']))})"
                   if _num(calendario.get("revenueLow")) is not None and _num(calendario.get("revenueHigh")) is not None else "")
            )
    return righe


def consenso_yahoo(ticker: str, scarica=scarica_sintesi_yahoo) -> str | None:
    righe = descrivi_sintesi(scarica(ticker))
    if not righe:
        return None
    oggi = datetime.now(timezone.utc).date().isoformat()
    return f"Analyst consensus and earnings history (Yahoo Finance, fetched {oggi}; amounts in the reporting currency):\n" + "\n".join(righe)


def prezzo_yahoo(ticker: str, scarica=scarica_yahoo) -> str | None:
    serie = scarica(ticker, "6mo")
    if not serie:
        return None
    ultimo_giorno, ultimo = serie[-1]
    riga = f"Stock {ticker}: last close {ultimo:,.2f} on {ultimo_giorno.isoformat()}"
    tre_mesi = [v for g, v in serie if g <= ultimo_giorno - timedelta(days=90)]
    if tre_mesi:
        riga += f", 3-month change {(ultimo - tre_mesi[-1]) / tre_mesi[-1]:+.1%}"
    return riga + " (context only: the share price does not resolve these questions)."


# ------------------------------------------------------------ orchestrazione
async def trimestrali(
    domanda,
    modello=None,
    sec=dati_sec,
    consenso=consenso_yahoo,
    prezzo=prezzo_yahoo,
) -> str | None:
    """Sezione del briefing con i dati trimestrali dell'azienda della domanda, o None se non c'è niente."""
    azienda = await identifica_azienda(domanda, modello)
    if azienda is None:
        logger.info("Trimestrali: nessuna azienda quotata riconosciuta nella domanda")
        return None
    nome, ticker = azienda
    sezioni: list[str] = []
    lette: list[str] = []
    for etichetta, funzione in (("SEC", sec), ("Yahoo consenso", consenso), ("Yahoo prezzo", prezzo)):
        try:
            testo = await asyncio.wait_for(asyncio.to_thread(funzione, ticker), timeout=TIMEOUT * 3)
        except Exception as errore:
            logger.warning(f"Trimestrali {etichetta} per {ticker} non letti: {type(errore).__name__}: {errore}")
            continue
        if testo:
            sezioni.append(testo)
            lette.append(etichetta)
    logger.info(f"Trimestrali per {ticker}: {', '.join(lette) or 'niente'}")
    if not sezioni:
        return None
    testa = (
        f"Structured quarterly data for ticker {ticker} (company: {nome}), fetched today. "
        "Check that the metric and fiscal period match the question exactly; revenue surprise history and "
        "guidance are not in these free feeds: take them from the web research."
    )
    return testa + "\n\n" + "\n\n".join(sezioni)
