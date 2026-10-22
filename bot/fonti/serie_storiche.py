"""
Serie storiche per le domande numeriche: FRED (API con FRED_API_KEY, altrimenti
il CSV pubblico senza chiave) e Yahoo Finance (endpoint "chart", lo stesso che
usa yfinance, senza dipendenze).

Un modello economico indica la serie (id FRED o ticker Yahoo); si scaricano
gli ultimi valori e si calcolano le variazioni su finestre lunghe quanto il
tempo che resta alla risoluzione: l'ultimo valore con la data, la variazione
tipica (deviazione standard), i percentili 5 e 95 delle variazioni, il minimo
e il massimo del periodo. Nel briefing finisce solo testo: la chiave FRED non
viene mai stampata.

Interruttore: SERIE_STORICHE=no. Costo: una chiamata al modello economico.
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
from datetime import date, datetime, timedelta, timezone

import requests

from bot import combina
from bot.fonti import fred

logger = logging.getLogger(__name__)

TIMEOUT = 15
INTESTAZIONI = {"User-Agent": "Mozilla/5.0 (compatible; bot-previsioni/2.0)"}
Osservazione = tuple[date, float]


# ------------------------------------------------------------ download
def scarica_fred(serie: str, chiave: str, limite: int = 400) -> list[Osservazione]:
    risposta = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": serie, "api_key": chiave, "file_type": "json", "sort_order": "desc", "limit": limite},
        headers=INTESTAZIONI,
        timeout=TIMEOUT,
    )
    risposta.raise_for_status()
    osservazioni = []
    for o in risposta.json().get("observations", []):
        try:
            osservazioni.append((date.fromisoformat(o["date"]), float(o["value"])))
        except (KeyError, ValueError):
            continue  # FRED scrive "." per i valori mancanti
    return sorted(osservazioni)


def scarica_yahoo(ticker: str, intervallo: str = "2y") -> list[Osservazione]:
    risposta = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"range": intervallo, "interval": "1d"},
        headers=INTESTAZIONI,
        timeout=TIMEOUT,
    )
    risposta.raise_for_status()
    risultato = (risposta.json().get("chart") or {}).get("result") or []
    if not risultato:
        raise ValueError(f"ticker Yahoo senza dati: {ticker}")
    tempi = risultato[0].get("timestamp") or []
    chiusure = ((risultato[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    osservazioni = [
        (datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c)) for t, c in zip(tempi, chiusure) if c is not None
    ]
    return sorted(osservazioni)


# ------------------------------------------------------------ statistiche
def _percentile(valori: list[float], q: float) -> float:
    ordinati = sorted(valori)
    if len(ordinati) == 1:
        return ordinati[0]
    posizione = q * (len(ordinati) - 1)
    i = int(posizione)
    frazione = posizione - i
    if i + 1 >= len(ordinati):
        return ordinati[-1]
    return ordinati[i] + (ordinati[i + 1] - ordinati[i]) * frazione


def variazioni_su_finestre(serie: list[Osservazione], giorni: int) -> list[float]:
    """Variazione (fine - inizio) su ogni finestra di circa `giorni` giorni nella serie."""
    if len(serie) < 2 or giorni <= 0:
        return []
    variazioni = []
    j = 0
    for i, (giorno_i, valore_i) in enumerate(serie):
        obiettivo = giorno_i + timedelta(days=giorni)
        while j < len(serie) and serie[j][0] < obiettivo:
            j += 1
        if j >= len(serie):
            break
        variazioni.append(serie[j][1] - valore_i)
    return variazioni


def descrivi(serie: list[Osservazione], nome: str, orizzonte_giorni: int, unita: str = "") -> str:
    """Testo per il briefing: ultimi valori e variabilità su finestre lunghe quanto l'orizzonte."""
    if not serie:
        return f"{nome}: no data."
    ultimi = serie[-6:]
    righe = [f"{nome} — latest value {ultimi[-1][1]:,.4g} {unita} on {ultimi[-1][0].isoformat()} (series has {len(serie)} points, from {serie[0][0].isoformat()})."]
    righe.append("Recent values: " + ", ".join(f"{g.isoformat()}: {v:,.4g}" for g, v in ultimi[:-1]))
    valori = [v for _, v in serie]
    righe.append(f"Period min {min(valori):,.4g}, max {max(valori):,.4g}, median {statistics.median(valori):,.4g}.")
    orizzonte = max(1, orizzonte_giorni)
    variazioni = variazioni_su_finestre(serie, orizzonte)
    if len(variazioni) >= 5:
        sd = statistics.pstdev(variazioni)
        righe.append(
            f"Changes over {orizzonte}-day windows (n={len(variazioni)}): mean {statistics.mean(variazioni):+,.4g}, "
            f"standard deviation {sd:,.4g}, 5th-95th percentile {_percentile(variazioni, 0.05):+,.4g} to "
            f"{_percentile(variazioni, 0.95):+,.4g}. A random-walk width for the time remaining is about "
            f"latest value ± {1.64 * sd:,.4g} (90% interval)."
        )
    else:
        righe.append(f"Too few {orizzonte}-day windows in the series to measure variability; use the period range.")
    return "\n".join(righe)


# ------------------------------------------------------------- orchestrazione
def _prompt_serie(domanda) -> str:
    # FRED si chiede sempre: con la chiave si usa l'API, senza il CSV pubblico.
    return (
        "SERIE_STORICHE\n"
        "Name the ONE data series that the question below will be resolved from, or the closest proxy with "
        'daily or monthly data. Give the FRED series id (for example "CPIAUCSL", "UNRATE", "DGS10") if an '
        'official statistic fits, and the Yahoo Finance ticker (for example "BZ=F" for Brent, "^GSPC", '
        '"BTC-USD", "EURUSD=X") if a traded price fits. Use null when nothing fits; do not guess identifiers you '
        "are not sure exist.\n\n"
        f"Question: {domanda.question_text}\nResolution criteria: {domanda.resolution_criteria}\n"
        f"Units: {getattr(domanda, 'unit_of_measure', None) or 'not stated'}\n\n"
        'Answer with one JSON block only: {"fred_series_id": "..." or null, "yahoo_ticker": "..." or null, "note": "..."}'
    )


def _pulisci_id(valore) -> str | None:
    if not isinstance(valore, str):
        return None
    valore = valore.strip().strip("\"'")
    if not valore or valore.lower() in {"null", "none", "n/a"} or len(valore) > 20 or " " in valore:
        return None
    return valore


async def serie_storiche(
    domanda, modello, scarica_fred_=scarica_fred, scarica_yahoo_=scarica_yahoo, scarica_fred_csv_=fred.scarica_csv
) -> str | None:
    """Sezione del briefing con le serie storiche, o None se nessuna serie è stata trovata."""
    chiave_fred = os.getenv("FRED_API_KEY", "").strip()
    con_chiave = bool(chiave_fred) and chiave_fred.upper() != "REPLACE_ME"
    risposta = await modello.invoke(_prompt_serie(domanda))
    oggetto = combina.leggi_json_finale(risposta) or {}
    id_fred = _pulisci_id(oggetto.get("fred_series_id"))
    ticker = _pulisci_id(oggetto.get("yahoo_ticker"))
    if not id_fred and not ticker:
        logger.info("Serie storiche: il modello non ha indicato nessuna serie")
        return None
    adesso = datetime.now(timezone.utc)
    fine = domanda.scheduled_resolution_time or domanda.close_time or adesso
    orizzonte = max(1, int((fine - adesso).total_seconds() // 86400))
    unita = getattr(domanda, "unit_of_measure", None) or ""
    sezioni = []
    compiti = []
    if id_fred and con_chiave:
        compiti.append((f"FRED series {id_fred}", scarica_fred_, (id_fred, chiave_fred)))
    elif id_fred:  # senza chiave: il CSV pubblico di FRED
        compiti.append((f"FRED series {id_fred} (public CSV)", scarica_fred_csv_, (id_fred,)))
    if ticker:
        compiti.append((f"Yahoo Finance {ticker}", scarica_yahoo_, (ticker,)))
    for nome, funzione, argomenti in compiti:
        try:
            serie = await asyncio.wait_for(asyncio.to_thread(funzione, *argomenti), timeout=TIMEOUT + 5)
            if serie:
                sezioni.append(descrivi(serie, nome, orizzonte, unita))
        except Exception as errore:
            logger.warning(f"Serie {nome} non letta: {type(errore).__name__}: {errore}")
    logger.info(f"Serie storiche: {len(sezioni)}/{len(compiti)} serie lette ({', '.join(n for n, _, _ in compiti)})")
    if not sezioni:
        return None
    nota = str(oggetto.get("note") or "").strip()[:200]
    testa = "Historical data series (fetched today; the suggested series may be a proxy, check it against the criteria)"
    if nota:
        testa += f" — assistant note: {nota}"
    return testa + ":\n" + "\n\n".join(sezioni)
