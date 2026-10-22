"""
FRED senza chiave: il CSV pubblico `fredgraph.csv?id=SERIE` (lo stesso che
scarica il bottone "Download" della pagina della serie). Le pagine
`fred.stlouisfed.org/series/SERIE` sono disegnate in JavaScript e lette come
testo risultano quasi vuote: da lì si passa sempre al CSV.

Usato dalla fonte di risoluzione (quando i criteri citano una serie FRED) e
dalle serie storiche quando manca FRED_API_KEY.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date

import requests

TIMEOUT = 15
INTESTAZIONI = {"User-Agent": "Mozilla/5.0 (compatible; bot-previsioni/2.0)"}
Osservazione = tuple[date, float]

_ID_DA_URL = re.compile(r"fred\.stlouisfed\.org/(?:series|data)/([A-Za-z0-9_\-]+)", flags=re.IGNORECASE)


def id_da_url(url: str) -> str | None:
    """L'id della serie da un URL `fred.stlouisfed.org/series/XXXX`, altrimenti None."""
    m = _ID_DA_URL.search(url or "")
    return m.group(1).split(".")[0].upper() if m else None


def leggi_csv(testo: str) -> list[Osservazione]:
    """Osservazioni (data, valore) in ordine di data dal CSV di FRED; i buchi (".") vengono saltati."""
    osservazioni: list[Osservazione] = []
    for riga in csv.reader(io.StringIO(testo)):
        if len(riga) < 2:
            continue
        try:
            osservazioni.append((date.fromisoformat(riga[0].strip()), float(riga[1])))
        except ValueError:
            continue  # intestazione o valore mancante
    return sorted(osservazioni)


def scarica_csv(serie: str, timeout: int = TIMEOUT) -> list[Osservazione]:
    """Tutta la serie dal CSV pubblico, senza chiave. Solleva se la risposta non è un CSV con dati."""
    risposta = requests.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": serie}, headers=INTESTAZIONI, timeout=timeout
    )
    risposta.raise_for_status()
    osservazioni = leggi_csv(risposta.text)
    if not osservazioni:
        raise ValueError(f"serie FRED senza osservazioni: {serie}")
    return osservazioni


def descrivi_ultime(serie: str, osservazioni: list[Osservazione], quante: int = 12) -> str:
    """Testo per il briefing: le ultime `quante` osservazioni con la data e l'ultimo valore in evidenza."""
    if not osservazioni:
        return f"FRED series {serie}: no data."
    ultime = osservazioni[-quante:]
    giorno, valore = ultime[-1]
    righe = [
        f"FRED series {serie} (public CSV, no key). LATEST VALUE: {valore:,.6g} on {giorno.isoformat()}.",
        f"Last {len(ultime)} observations (series has {len(osservazioni)} points, from {osservazioni[0][0].isoformat()}):",
    ]
    righe += [f"- {g.isoformat()}: {v:,.6g}" for g, v in ultime]
    return "\n".join(righe)
