"""
Mercati di previsione come evidenza, pesati per liquidità.

- Ricerca testuale su Polymarket (Gamma API), Manifold e Kalshi (API pubbliche,
  senza chiave), con timeout e fallimenti non bloccanti.
- L'abbinamento domanda <-> mercato lo fa un modello economico (il matching
  fuzzy sbaglia): "same" = stesso evento, stessi criteri, data confrontabile;
  "related" = informativo ma con criteri o date diverse.
- Peso per liquidità (nostreambot): 0 a 1.000 $ di volume, 0,04 a 10.000 $,
  pieno verso 100.000 $. Sotto 0,5 l'evidenza è "weak", mai un'àncora.
- Proiezione alla nostra data con hazard costante quando le date differiscono.

Interruttore: MERCATI=no. Costo: una chiamata al modello economico per domanda.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from bot import combina

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_PER_PIATTAFORMA = 5
INTESTAZIONI = {"User-Agent": "Mozilla/5.0 (compatible; bot-previsioni/2.0)", "Accept": "application/json"}
STOPWORD = {
    "will", "the", "a", "an", "of", "in", "on", "at", "by", "to", "be", "is", "are", "for", "and", "or", "than",
    "before", "after", "with", "from", "as", "its", "it", "this", "that", "what", "which", "who", "how", "many",
    "much", "does", "do", "did", "have", "has", "between", "during", "any", "there", "their", "into", "over",
    "more", "less", "least", "most", "per", "end", "2025", "2026", "2027", "question", "resolve", "resolves",
}


@dataclass
class Mercato:
    piattaforma: str
    titolo: str
    prezzo_si: float | None  # probabilità implicita del "sì" (0-1), None se non binario
    volume_usd: float
    chiusura: datetime | None
    url: str
    dettaglio: str = ""  # per esempio l'opzione di un mercato a più esiti


# ------------------------------------------------------------------ pesi puri
def peso_liquidita(volume_usd: float | None) -> float:
    """0 fino a 1k $, 0,04 a 10k $, 1,0 da 100k $ in su; lineare in log10 tra i punti."""
    if not volume_usd or volume_usd <= 1_000:
        return 0.0
    if volume_usd >= 100_000:
        return 1.0
    x = math.log10(volume_usd)
    if x <= 4:
        return 0.04 * (x - 3)
    return 0.04 + (1.0 - 0.04) * (x - 4)


def forza(peso: float) -> str:
    if peso <= 0:
        return "negligible (ignore for anchoring)"
    return "strong" if peso >= 0.5 else "weak"


def proietta_hazard(prezzo: float, giorni_mercato: float, giorni_nostri: float) -> float:
    """
    Prezzo di un mercato "entro la data X" riportato alla nostra data con
    hazard costante: p = 1 - exp(-h t). Fuori dai casi sensati restituisce il prezzo.
    """
    if not (0 < prezzo < 1) or giorni_mercato <= 0 or giorni_nostri <= 0:
        return prezzo
    h = -math.log(1 - prezzo) / giorni_mercato
    return 1 - math.exp(-h * giorni_nostri)


def parole_chiave(testo: str, massimo: int = 6) -> list[str]:
    parole = re.findall(r"[A-Za-z][A-Za-z0-9\-\.']{2,}", testo or "")
    uniche: list[str] = []
    for p in parole:
        basso = p.lower().strip(".'")
        if basso in STOPWORD or basso in uniche:
            continue
        uniche.append(basso)
    # Le parole con la maiuscola (nomi propri, sigle) prima.
    proprie = [p for p in uniche if any(c.isupper() for c in next((w for w in parole if w.lower().strip(".'") == p), ""))]
    altre = [p for p in uniche if p not in proprie]
    return (proprie + altre)[:massimo]


# ------------------------------------------------------------- piattaforme
def _data(valore) -> datetime | None:
    if valore in (None, ""):
        return None
    try:
        if isinstance(valore, (int, float)):
            return datetime.fromtimestamp(valore / (1000 if valore > 1e11 else 1), tz=timezone.utc)
        return datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None


def _get(url: str, **params) -> object:
    risposta = requests.get(url, params=params, headers=INTESTAZIONI, timeout=TIMEOUT)
    risposta.raise_for_status()
    return risposta.json()


def cerca_manifold(termine: str) -> list[Mercato]:
    dati = _get("https://api.manifold.markets/v0/search-markets", term=termine, limit=MAX_PER_PIATTAFORMA, filter="open")
    mercati = []
    for m in dati if isinstance(dati, list) else []:
        if m.get("outcomeType") != "BINARY" or m.get("isResolved"):
            continue
        mercati.append(
            Mercato(
                "Manifold (play money)", str(m.get("question") or ""), m.get("probability"),
                float(m.get("volume") or 0), _data(m.get("closeTime")), str(m.get("url") or ""),
            )
        )
    return mercati


def cerca_polymarket(termine: str) -> list[Mercato]:
    dati = _get("https://gamma-api.polymarket.com/public-search", q=termine, limit_per_type=MAX_PER_PIATTAFORMA)
    mercati: list[Mercato] = []
    eventi = dati.get("events") if isinstance(dati, dict) else []
    for evento in eventi or []:
        for m in evento.get("markets") or []:
            if m.get("closed"):
                continue
            prezzi = m.get("outcomePrices")
            esiti = m.get("outcomes")
            try:
                if isinstance(prezzi, str):
                    import json

                    prezzi = json.loads(prezzi)
                    esiti = json.loads(esiti) if isinstance(esiti, str) else esiti
                prezzo = float(prezzi[0]) if prezzi else None
            except (ValueError, TypeError, IndexError):
                prezzo = None
            volume = float(m.get("volumeNum") or m.get("volume") or evento.get("volume") or 0)
            dettaglio = ""
            if esiti and str(esiti[0]).lower() != "yes":
                dettaglio = f"price is for outcome '{esiti[0]}'"
            slug = m.get("slug") or evento.get("slug") or ""
            mercati.append(
                Mercato(
                    "Polymarket", str(m.get("question") or evento.get("title") or ""), prezzo, volume,
                    _data(m.get("endDate") or evento.get("endDate")), f"https://polymarket.com/market/{slug}", dettaglio,
                )
            )
            if len(mercati) >= MAX_PER_PIATTAFORMA:
                return mercati
    return mercati


def cerca_kalshi(termine: str) -> list[Mercato]:
    """Kalshi non ha ricerca testuale pubblica: si leggono i mercati aperti e si filtrano per parole chiave."""
    chiavi = [p for p in parole_chiave(termine, 8)]
    if not chiavi:
        return []
    dati = _get("https://api.elections.kalshi.com/trade-api/v2/markets", status="open", limit=200)
    mercati: list[Mercato] = []
    for m in (dati.get("markets") if isinstance(dati, dict) else []) or []:
        titolo = " ".join(str(m.get(k) or "") for k in ("title", "subtitle", "yes_sub_title"))
        basso = titolo.lower()
        if sum(1 for c in chiavi if c in basso) < 2:
            continue
        prezzo = m.get("last_price") if m.get("last_price") is not None else m.get("yes_ask")
        prezzo = float(prezzo) / 100 if prezzo is not None else None
        volume = float(m.get("volume") or 0)  # contratti da 1 $: ordine di grandezza del volume in dollari
        mercati.append(
            Mercato(
                "Kalshi", titolo.strip(), prezzo, volume, _data(m.get("close_time") or m.get("expiration_time")),
                f"https://kalshi.com/markets/{m.get('event_ticker') or m.get('ticker')}",
            )
        )
        if len(mercati) >= MAX_PER_PIATTAFORMA:
            break
    return mercati


PIATTAFORME = {"Manifold": cerca_manifold, "Polymarket": cerca_polymarket, "Kalshi": cerca_kalshi}


async def cerca_tutti(termine: str, piattaforme: dict | None = None) -> list[Mercato]:
    piattaforme = PIATTAFORME if piattaforme is None else piattaforme

    async def una(nome, funzione):
        try:
            return await asyncio.wait_for(asyncio.to_thread(funzione, termine), timeout=TIMEOUT + 5)
        except Exception as errore:
            logger.warning(f"Mercati {nome} non letti: {type(errore).__name__}: {errore}")
            return []

    risultati = await asyncio.gather(*(una(n, f) for n, f in piattaforme.items()))
    return [m for lista in risultati for m in lista]


# ------------------------------------------------------------ abbinamento
def _prompt_abbinamento(domanda, candidati: list[Mercato]) -> str:
    righe = []
    for i, m in enumerate(candidati, 1):
        chiusura = m.chiusura.strftime("%Y-%m-%d") if m.chiusura else "unknown"
        righe.append(f"{i}. [{m.piattaforma}] {m.titolo} (closes {chiusura}) {m.dettaglio}".rstrip())
    risoluzione = domanda.scheduled_resolution_time.strftime("%Y-%m-%d") if domanda.scheduled_resolution_time else "unknown"
    return (
        "ABBINAMENTO_MERCATI\n"
        "You match a forecasting question to prediction markets. Be strict: 'same' only if the market "
        "resolves on the same event with the same criteria and a comparable date; 'related' if it is "
        "informative but differs in criteria, threshold or date. Everything else is not a match.\n\n"
        f"Question: {domanda.question_text}\nResolution criteria: {domanda.resolution_criteria}\n"
        f"Resolution date: {risoluzione}\n\nCandidate markets:\n" + "\n".join(righe) + "\n\n"
        'Answer with one JSON block only: {"matches": [{"n": 1, "relation": "same", "why": "..."}]} '
        '(use "related" where appropriate; empty list if nothing matches).'
    )


def _leggi_abbinamenti(risposta: str, quanti: int) -> list[tuple[int, str, str]]:
    oggetto = combina.leggi_json_finale(risposta) or {}
    voci = oggetto.get("matches") if isinstance(oggetto, dict) else None
    letti: list[tuple[int, str, str]] = []
    for voce in voci or []:
        try:
            n = int(voce.get("n"))
            relazione = str(voce.get("relation", "")).strip().lower()
        except (AttributeError, TypeError, ValueError):
            continue
        if 1 <= n <= quanti and relazione in {"same", "related"}:
            letti.append((n, relazione, str(voce.get("why", ""))[:160]))
    return letti


def _riga_evidenza(m: Mercato, relazione: str, perche: str, data_nostra: datetime | None) -> str:
    peso = peso_liquidita(m.volume_usd)
    chiusura = m.chiusura.strftime("%Y-%m-%d") if m.chiusura else "unknown"
    testo = f"- [{m.piattaforma}] {m.titolo} — match: {relazione}"
    if perche:
        testo += f" ({perche})"
    if m.prezzo_si is None:
        testo += f". Price not available. Volume about ${m.volume_usd:,.0f}. {m.url}"
        return testo
    testo += f". Price {m.prezzo_si:.0%} yes, volume about ${m.volume_usd:,.0f}, closes {chiusura}. Evidence: {forza(peso)} (liquidity weight {peso:.2f})."
    adesso = datetime.now(timezone.utc)
    if relazione == "same" and m.chiusura and data_nostra and 0 < peso:
        giorni_mercato = (m.chiusura - adesso).total_seconds() / 86400
        giorni_nostri = (data_nostra - adesso).total_seconds() / 86400
        if giorni_mercato > 0 and giorni_nostri > 0 and abs(giorni_mercato - giorni_nostri) > 2:
            proiettato = proietta_hazard(m.prezzo_si, giorni_mercato, giorni_nostri)
            testo += f" Projected to our resolution date with a constant hazard: {proiettato:.0%}."
    if m.dettaglio:
        testo += f" Note: {m.dettaglio}."
    testo += f" {m.url}"
    return testo


async def evidenza_mercati(domanda, modello_abbinamento, cerca=cerca_tutti) -> str | None:
    """Sezione del briefing sui mercati abbinati, o None se non c'è niente di utile."""
    termine = " ".join(parole_chiave(domanda.question_text))
    if not termine:
        return None
    candidati = await cerca(termine)
    candidati = [c for c in candidati if c.titolo][:3 * MAX_PER_PIATTAFORMA]
    if not candidati:
        return None
    risposta = await modello_abbinamento.invoke(_prompt_abbinamento(domanda, candidati))
    abbinamenti = _leggi_abbinamenti(risposta, len(candidati))
    if not abbinamenti:
        return (
            f"Prediction markets: {len(candidati)} candidate markets found for '{termine}', none judged to be on "
            "this question (no market evidence)."
        )
    righe = [
        "Prediction markets (matched by an assistant; weight by liquidity, a thin market is weak evidence, never an anchor):"
    ]
    for n, relazione, perche in sorted(abbinamenti, key=lambda a: (a[1] != "same", a[0])):
        righe.append(_riga_evidenza(candidati[n - 1], relazione, perche, domanda.scheduled_resolution_time))
    return "\n".join(righe)
