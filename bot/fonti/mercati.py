"""
Mercati di previsione come evidenza, pesati per liquidità.

- Query di ricerca: il modello economico scrive da 1 a 3 query corte (2-4 parole,
  nomi propri ed entità, ASCII senza accenti: "El Nino", "Swiss drought"). La
  ricerca di Manifold vuole TUTTE le parole del termine, quindi un termine lungo
  non trova niente. Se il modello non risponde si usano le parole chiave del
  titolo (accenti tolti, al massimo 3 parole, nomi propri prima).
- Ogni piattaforma viene interrogata con ogni query; i candidati si uniscono
  senza doppioni, al massimo 5 per piattaforma (15 in tutto, come prima).
- Polymarket (Gamma API) e Manifold hanno la ricerca testuale. Kalshi no: si
  legge la lista degli eventi aperti (a pagine, una volta ogni mezz'ora per
  tutto il giro) e la si filtra con le parole delle query. API pubbliche senza
  chiave, con timeout e fallimenti non bloccanti.
- L'abbinamento domanda <-> mercato lo fa un modello economico (il matching
  fuzzy sbaglia): "same" = stesso evento, stessi criteri, data confrontabile;
  "related" = informativo ma con criteri o date diverse.
- Peso per liquidità (nostreambot): 0 a 1.000 $ di volume, 0,04 a 10.000 $,
  pieno verso 100.000 $. Sotto 0,5 l'evidenza è "weak", mai un'àncora.
- Proiezione alla nostra data con hazard costante quando le date differiscono.

Interruttori: MERCATI=no (tutti i mercati), KALSHI=no (solo Kalshi).
Costo: due chiamate al modello economico per domanda (query e abbinamento).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from bot import combina
from bot.spese import TettoSuperato

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_PER_PIATTAFORMA = 5
MAX_CANDIDATI = 3 * MAX_PER_PIATTAFORMA  # tetto totale dei candidati mandati all'abbinamento
MAX_QUERY = 3
MAX_PAROLE_QUERY = 4
INTESTAZIONI = {"User-Agent": "Mozilla/5.0 (compatible; bot-previsioni/2.0)", "Accept": "application/json"}
STOPWORD = {
    # articoli, pronomi, verbi ausiliari, preposizioni e congiunzioni
    "a", "an", "the", "this", "that", "these", "those", "there", "their", "they", "them", "it", "its", "he", "she",
    "his", "her", "him", "we", "our", "you", "your", "who", "whom", "whose", "what", "which", "when", "where", "why",
    "how", "will", "would", "shall", "should", "can", "could", "may", "might", "must", "be", "been", "being", "is",
    "are", "was", "were", "do", "does", "did", "done", "have", "has", "had", "get", "gets", "got", "of", "in", "on",
    "at", "by", "to", "for", "from", "with", "without", "within", "into", "onto", "over", "under", "above", "below",
    "between", "among", "through", "during", "before", "after", "until", "since", "about", "against", "than", "then",
    "as", "or", "and", "nor", "but", "if", "so", "not", "no", "yes", "either", "neither", "both", "each", "every",
    "any", "all", "some", "none", "many", "much", "more", "most", "less", "least", "few", "fewer", "other", "others",
    "another", "such", "same", "own", "only", "also", "still", "ever", "again", "very", "just", "per", "vs", "via",
    # parole generiche delle domande di previsione
    "question", "questions", "resolve", "resolves", "resolved", "resolution", "metaculus", "according", "official",
    "officially", "classified", "announce", "announced", "announces", "report", "reported", "reports", "publish",
    "published", "happen", "happens", "occur", "occurs", "take", "place", "new", "next", "first", "last", "end",
    "start", "begin", "higher", "lower", "greater", "exceed", "exceeds", "exceeded", "reach", "reaches", "reached",
    "receive", "received", "win", "wins", "won", "become", "becomes", "number", "total", "percent", "percentage",
    "value", "level", "amount", "times", "time", "year", "years", "month", "months", "week", "weeks", "day", "days",
    "date", "dates", "extremely",
    # mesi e giorni
    "january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
    "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
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


# ------------------------------------------------------------------ testo
_SOSTITUZIONI = str.maketrans(
    {"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "đ": "d", "ł": "l", "Ł": "L"}
)


def senza_accenti(testo: str) -> str:
    """'El Niño' -> 'El Nino': NFKD, via i segni diacritici, poi via quel che resta fuori dall'ASCII."""
    scomposto = unicodedata.normalize("NFKD", (testo or "").translate(_SOSTITUZIONI))
    return "".join(c for c in scomposto if not unicodedata.combining(c)).encode("ascii", "ignore").decode("ascii")


def parole_chiave(testo: str, massimo: int = 3) -> list[str]:
    """
    Riserva senza modello: al massimo `massimo` parole per la ricerca, accenti tolti.
    Prima i nomi propri (maiuscola iniziale); se ce ne sono almeno 2 bastano loro,
    perché Manifold vuole tutte le parole del termine e meno parole trovano di più.
    Altrimenti si aggiungono le parole più lunghe, che sono le più specifiche.
    """
    proprie: list[str] = []
    altre: list[str] = []
    viste: set[str] = set()
    for parola in re.findall(r"[A-Za-z][A-Za-z0-9']*[A-Za-z0-9]", senza_accenti(testo)):
        parola = re.sub(r"'s$", "", parola)
        basso = parola.lower()
        if len(parola) < 2 or basso in STOPWORD or basso in viste:
            continue
        viste.add(basso)
        (proprie if parola[0].isupper() else altre).append(parola)
    scelte = proprie[:massimo]
    if len(scelte) < 2:
        scelte += sorted(altre, key=len, reverse=True)[: massimo - len(scelte)]
    return scelte


def pulisci_query(testo) -> str:
    """Una query pulita: ASCII senza accenti, niente punteggiatura strana, al massimo 4 parole."""
    if not isinstance(testo, str):
        return ""
    parole = re.sub(r"[^A-Za-z0-9&'.\- ]", " ", senza_accenti(testo)).split()
    return " ".join(parole[:MAX_PAROLE_QUERY])


def _parole_query(query: str) -> list[str]:
    """Parole significative di una query, minuscole, per filtrare i titoli (Kalshi)."""
    parole = re.findall(r"[a-z0-9]+", senza_accenti(query).lower())
    return [p for p in dict.fromkeys(parole) if len(p) >= 2 and p not in STOPWORD]


# ------------------------------------------------------------ query di ricerca
def _prompt_query(domanda) -> str:
    criteri = str(domanda.resolution_criteria or "")[:1500]
    return (
        "QUERY_MERCATI\n"
        "Write 1 to 3 short search queries to find prediction markets (Polymarket, Manifold, Kalshi) about the "
        "forecasting question below. Each query has 2 to 4 words, built from proper names and entities (people, "
        "places, organizations, named indices or events), in plain ASCII without accents. No dates, numbers, "
        "thresholds or generic words such as will, higher, more, votes. Put the most specific query first; the "
        "others can be broader or use another common name for the same thing. Examples: \"El Nino\", "
        "\"Nevada None of These Candidates\", \"Swiss drought\".\n\n"
        f"Question: {domanda.question_text}\nResolution criteria: {criteri}\n\n"
        'Answer with one JSON block only: {"queries": ["...", "..."]}'
    )


def _leggi_query(risposta: str) -> list[str]:
    oggetto = combina.leggi_json_finale(risposta) or {}
    voci = oggetto.get("queries") if isinstance(oggetto, dict) else None
    if isinstance(voci, str):
        voci = [voci]
    query: list[str] = []
    for voce in voci if isinstance(voci, list) else []:
        pulita = pulisci_query(voce)
        if pulita and pulita.lower() not in {q.lower() for q in query}:
            query.append(pulita)
    return query[:MAX_QUERY]


async def query_di_ricerca(domanda, modello) -> list[str]:
    """
    Da 1 a 3 query corte scritte dal modello economico (una chiamata, registrata nel
    registro come le altre); se il modello fallisce o non scrive niente di usabile,
    le parole chiave del titolo. Un tetto di spesa superato ferma la domanda.
    """
    query: list[str] = []
    try:
        query = _leggi_query(await modello.invoke(_prompt_query(domanda)))
        if not query:
            logger.warning("Query dei mercati: il modello non ne ha scritte di usabili, uso le parole chiave")
    except TettoSuperato:
        raise
    except Exception as errore:
        logger.warning(f"Query dei mercati dal modello fallite ({type(errore).__name__}: {errore}), uso le parole chiave")
    if not query:
        riserva = " ".join(parole_chiave(domanda.question_text))
        query = [riserva] if riserva else []
    return query


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


def _numero(valore) -> float | None:
    try:
        numero = float(valore)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _get(url: str, **params) -> object:
    risposta = requests.get(url, params=params, headers=INTESTAZIONI, timeout=TIMEOUT)
    risposta.raise_for_status()
    return risposta.json()


def cerca_manifold(termine: str) -> list[Mercato]:
    # Si chiedono più risultati del necessario: il limite vale prima del filtro sui
    # mercati binari, e i mercati a scelta multipla occupavano tutti i posti.
    dati = _get(
        "https://api.manifold.markets/v0/search-markets", term=termine, limit=4 * MAX_PER_PIATTAFORMA, filter="open"
    )
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
        if len(mercati) >= MAX_PER_PIATTAFORMA:
            break
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


# Kalshi. Prima si leggevano 200 mercati aperti su decine di migliaia (quasi tutti
# combinazioni sportive) e se ne tenevano quelli con 2 parole chiave: mai niente.
# Ora si leggono gli EVENTI aperti (l'endpoint /events lascia fuori i multivariati),
# solo quelli che chiudono oltre le prossime 24 ore, a pagine da 200 con il cursore.
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PAGINE = 15  # fino a 3.000 eventi aperti
KALSHI_SECONDI = 8  # tempo massimo per scaricare la lista
KALSHI_VALIDITA = 1800  # la lista vale mezz'ora: tutte le domande del giro usano la stessa
KALSHI_RIPROVA = 300  # dopo un errore sulla prima pagina si riprova tra 5 minuti
KALSHI_EVENTI_PER_QUERY = 3
KALSHI_MERCATI_PER_EVENTO = 3
_kalshi_lucchetto = threading.Lock()
_kalshi_eventi: dict = {"momento": None, "lista": []}
_kalshi_mercati: dict[str, tuple[float, list]] = {}


def azzera_cache_kalshi() -> None:
    with _kalshi_lucchetto:
        _kalshi_eventi.update(momento=None, lista=[])
        _kalshi_mercati.clear()


def eventi_kalshi() -> list[dict]:
    """
    Eventi aperti di Kalshi, scaricati una volta ogni mezz'ora. Se la prima pagina
    fallisce l'errore sale e finisce nel log una volta sola: per 5 minuti la lista
    resta vuota e non si riprova. Se cade una pagina successiva si tiene quello che
    si è letto.
    """
    with _kalshi_lucchetto:
        adesso = time.monotonic()
        if _kalshi_eventi["momento"] is not None and adesso - _kalshi_eventi["momento"] < KALSHI_VALIDITA:
            return _kalshi_eventi["lista"]
        _kalshi_eventi.update(momento=adesso - KALSHI_VALIDITA + KALSHI_RIPROVA, lista=[])
        lista: list[dict] = []
        cursore = None
        parametri = {"status": "open", "limit": 200, "min_close_ts": int(time.time()) + 86400}
        for _ in range(KALSHI_PAGINE):
            try:
                dati = _get(f"{KALSHI_API}/events", **parametri, **({"cursor": cursore} if cursore else {}))
            except Exception as errore:
                rifiutato = getattr(getattr(errore, "response", None), "status_code", None) == 400
                if not lista and rifiutato and parametri.pop("min_close_ts", None):
                    continue  # filtro sulla chiusura non accettato: si riprova con la lista intera
                if not lista:
                    raise
                logger.warning(f"Kalshi: lista degli eventi interrotta a {len(lista)} ({type(errore).__name__})")
                break
            pagina = (dati.get("events") if isinstance(dati, dict) else None) or []
            lista += [e for e in pagina if isinstance(e, dict)]
            cursore = dati.get("cursor") if isinstance(dati, dict) else None
            if not pagina or not cursore or time.monotonic() - adesso > KALSHI_SECONDI:
                break
        _kalshi_eventi.update(momento=adesso, lista=lista)
        return lista


def punteggio_evento(parole: list[str], evento: dict) -> float:
    """Quota delle parole della query presenti nel titolo dell'evento (0 se troppo poche)."""
    if not parole:
        return 0.0
    titolo = " ".join(str(evento.get(k) or "") for k in ("title", "sub_title"))
    nel_titolo = set(re.findall(r"[a-z0-9]+", senza_accenti(titolo).lower()))
    trovate = sum(1 for p in parole if p in nel_titolo)
    # Con 1-2 parole servono tutte ("El Nino" non deve prendere "El Clasico"), con 3-4 almeno 2.
    servono = len(parole) if len(parole) <= 2 else 2
    return trovate / len(parole) if trovate >= servono else 0.0


def _mercati_evento(ticker: str) -> list[dict]:
    salvati = _kalshi_mercati.get(ticker)
    if salvati and time.monotonic() - salvati[0] < KALSHI_VALIDITA:
        return salvati[1]
    dati = _get(f"{KALSHI_API}/markets", event_ticker=ticker, status="open", limit=100)
    mercati = [m for m in ((dati.get("markets") if isinstance(dati, dict) else None) or []) if isinstance(m, dict)]
    _kalshi_mercati[ticker] = (time.monotonic(), mercati)
    return mercati


def _in_dollari(m: dict, nome: str) -> float | None:
    """Prezzo in dollari (0-1): il campo nuovo *_dollars, altrimenti i vecchi centesimi diviso 100."""
    valore = _numero(m.get(f"{nome}_dollars"))
    if valore is None:
        centesimi = _numero(m.get(nome))
        valore = centesimi / 100 if centesimi is not None else None
    return valore if valore is not None and 0 <= valore <= 1 else None


def prezzo_kalshi(m: dict) -> float | None:
    ultimo = _in_dollari(m, "last_price")
    if ultimo:  # 0 = mai scambiato
        return ultimo
    denaro, lettera = _in_dollari(m, "yes_bid"), _in_dollari(m, "yes_ask")
    if denaro is not None and lettera is not None and denaro < lettera and lettera - denaro <= 0.10:
        return (denaro + lettera) / 2  # punto medio solo se la forbice è stretta
    return None


def _volume_kalshi(m: dict) -> float:
    # Contratti da 1 $: il numero di contratti è l'ordine di grandezza del volume in dollari.
    return _numero(m.get("volume_fp")) or _numero(m.get("volume")) or 0.0


def cerca_kalshi(termine: str) -> list[Mercato]:
    """Kalshi non ha ricerca testuale pubblica: si filtrano gli eventi aperti e si leggono i mercati dei migliori."""
    parole = _parole_query(termine)
    if not parole:
        return []
    punteggi = [(punteggio_evento(parole, e), i, e) for i, e in enumerate(eventi_kalshi())]
    migliori = sorted((p for p in punteggi if p[0] > 0), key=lambda p: (-p[0], p[1]))[:KALSHI_EVENTI_PER_QUERY]
    mercati: list[Mercato] = []
    for _, _, evento in migliori:
        ticker = evento.get("event_ticker")
        if not ticker:
            continue
        for m in sorted(_mercati_evento(ticker), key=_volume_kalshi, reverse=True)[:KALSHI_MERCATI_PER_EVENTO]:
            titolo = " ".join(str(m.get(k)).strip() for k in ("title", "subtitle", "yes_sub_title") if m.get(k))
            mercati.append(
                Mercato(
                    "Kalshi", titolo or str(evento.get("title") or ""), prezzo_kalshi(m), _volume_kalshi(m),
                    _data(m.get("close_time") or m.get("expiration_time")), f"https://kalshi.com/markets/{ticker}",
                )
            )
            if len(mercati) >= MAX_PER_PIATTAFORMA:
                return mercati
    return mercati


PIATTAFORME = {"Manifold": cerca_manifold, "Polymarket": cerca_polymarket, "Kalshi": cerca_kalshi}


def piattaforme_attive(kalshi: bool = True) -> dict:
    """Le piattaforme da interrogare: KALSHI=no toglie Kalshi."""
    return {nome: funzione for nome, funzione in PIATTAFORME.items() if kalshi or nome != "Kalshi"}


def unisci(liste: list[list[Mercato]], massimo: int = MAX_PER_PIATTAFORMA) -> list[Mercato]:
    """I risultati delle query di una piattaforma, a turno (il primo di ogni query, poi il secondo...), senza doppioni."""
    uniti: list[Mercato] = []
    visti: set[tuple] = set()
    for posizione in range(max((len(lista) for lista in liste), default=0)):
        for lista in liste:
            if posizione >= len(lista):
                continue
            m = lista[posizione]
            chiave = (m.piattaforma, m.titolo.strip().lower(), m.dettaglio, m.url)
            if chiave in visti:
                continue
            visti.add(chiave)
            uniti.append(m)
            if len(uniti) >= massimo:
                return uniti
    return uniti


async def cerca_tutti(query: list[str] | str, piattaforme: dict | None = None) -> list[Mercato]:
    piattaforme = PIATTAFORME if piattaforme is None else piattaforme
    query = [query] if isinstance(query, str) else list(query)
    errori: dict[str, str] = {}

    async def una(nome, funzione, termine):
        try:
            return await asyncio.wait_for(asyncio.to_thread(funzione, termine), timeout=TIMEOUT + 5)
        except Exception as errore:
            errori.setdefault(nome, f"{type(errore).__name__}: {errore}")
            return []

    compiti = [(nome, termine) for nome in piattaforme for termine in query]
    risultati = await asyncio.gather(*(una(nome, piattaforme[nome], termine) for nome, termine in compiti))
    per_piattaforma: dict[str, list[list[Mercato]]] = {nome: [] for nome in piattaforme}
    for (nome, _), lista in zip(compiti, risultati):
        per_piattaforma[nome].append(lista)
    uniti = {nome: unisci(liste) for nome, liste in per_piattaforma.items()}
    for nome, errore in errori.items():  # un avviso per piattaforma, non uno per query
        logger.warning(f"Mercati {nome} non letti: {errore}")
    conteggi = ", ".join(f"{nome} {len(lista)}" for nome, lista in uniti.items())
    logger.info(f"Mercati per '{' | '.join(query)}': {sum(len(lista) for lista in uniti.values())} trovati ({conteggi})")
    return [m for lista in uniti.values() for m in lista]


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


async def evidenza_mercati(domanda, modello, cerca=cerca_tutti) -> str | None:
    """
    Sezione del briefing sui mercati abbinati, o None se non c'è niente di utile.
    Il modello economico scrive le query e poi giudica i candidati; `cerca` riceve
    la lista delle query.
    """
    query = await query_di_ricerca(domanda, modello)
    if not query:
        return None
    termine = " | ".join(query)
    candidati = await cerca(query)
    candidati = [c for c in candidati if c.titolo][:MAX_CANDIDATI]
    if not candidati:
        logger.info(f"Mercati: nessun candidato per '{termine}'")
        return None
    risposta = await modello.invoke(_prompt_abbinamento(domanda, candidati))
    abbinamenti = _leggi_abbinamenti(risposta, len(candidati))
    stessi = sum(1 for _, relazione, _ in abbinamenti if relazione == "same")
    logger.info(
        f"Mercati: {len(candidati)} candidati, {len(abbinamenti)} abbinati "
        f"({stessi} same, {len(abbinamenti) - stessi} related)"
    )
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
