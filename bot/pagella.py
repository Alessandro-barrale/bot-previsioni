"""
Pagella viva: le nostre previsioni contro la community prediction, domanda per domanda.

Con METACULUS_TOKEN scarica i post risolti su cui il bot ha previsto, prende
l'ultima nostra previsione prima della chiusura (lo "spot") e la community
prediction nello stesso momento, poi calcola:
- log score nostro e della community (ln della probabilità data all'esito;
  per numeriche e date la massa del bin della CDF in cui cade l'esito);
- lo spot peer score STIMATO: 100 x (log nostro - log community). È una stima
  prudente: il peer score vero confronta con la media dei log score degli
  altri bot, che è più bassa del log score del loro aggregato.

Tutto finisce in dati/pagella.sqlite (una riga per domanda, aggiornata a ogni
giro) e nel rapporto del lunedì. Senza token non si scarica niente; le
funzioni di calcolo sono pure e coperte dai test con dati finti.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from bot.config import CARTELLA_PROGETTO

logger = logging.getLogger(__name__)

API = "https://www.metaculus.com/api"
PERCORSO_PAGELLA = CARTELLA_PROGETTO / "dati" / "pagella.sqlite"
TIMEOUT = 30
PROB_MINIMA = 1e-4  # per non fare ln(0) su previsioni degeneri


@dataclass
class RigaPagella:
    domanda: str  # "post-question", come nel registro
    post: int
    titolo: str
    torneo: str
    tipo: str
    chiusura: str
    risolto_il: str | None
    risoluzione: str
    nostra: str  # testo breve della nostra previsione (p, opzione, mediana)
    community: str
    log_nostro: float
    log_community: float
    peer_stimato: float


# ------------------------------------------------------------- calcolo puro
def _ultimo_prima(storia: list[dict], istante: float) -> dict | None:
    """L'ultima voce di una storia (start_time crescente) iniziata entro l'istante dato."""
    valide = [v for v in storia or [] if v.get("start_time") is not None and float(v["start_time"]) <= istante]
    if not valide:
        return None
    return max(valide, key=lambda v: float(v["start_time"]))


def _log(p: float) -> float:
    return math.log(max(PROB_MINIMA, min(1.0, float(p))))


def _posizione(valore: float, scaling: dict) -> float:
    """Posizione 0-1 di un valore nominale sulla scala della domanda (lineare o logaritmica)."""
    minimo = float(scaling["range_min"])
    massimo = float(scaling["range_max"])
    zero = scaling.get("zero_point")
    if zero is not None:
        zero = float(zero)
        rapporto = (massimo - zero) / (minimo - zero)
        return math.log((valore - zero) / (minimo - zero)) / math.log(rapporto)
    return (valore - minimo) / (massimo - minimo)


def _valore_risoluzione(risoluzione: str, tipo: str) -> float | str:
    """Numero (o timestamp per le date) dalla stringa di risoluzione; oppure 'below'/'above'."""
    testo = str(risoluzione).strip().lower()
    if testo in {"below_lower_bound", "below"}:
        return "below"
    if testo in {"above_upper_bound", "above"}:
        return "above"
    if tipo == "date":
        return datetime.fromisoformat(str(risoluzione).replace("Z", "+00:00")).timestamp()
    return float(str(risoluzione).replace(",", ""))


def massa_del_bin(cdf: list[float], risoluzione: str, tipo: str, scaling: dict) -> float:
    """
    Probabilità che una CDF (201 punti, o inbound_outcome_count + 1 per le
    discrete) assegna all'esito: il bin in cui cade, oppure la massa oltre il
    limite se l'esito è fuori scala.
    """
    valore = _valore_risoluzione(risoluzione, tipo)
    if valore == "below":
        return cdf[0]
    if valore == "above":
        return 1.0 - cdf[-1]
    posizione = _posizione(float(valore), scaling)
    bins = len(cdf) - 1
    if posizione < 0:
        return cdf[0]
    if posizione > 1:
        return 1.0 - cdf[-1]
    indice = min(bins - 1, int(math.floor(posizione * bins)))
    return cdf[indice + 1] - cdf[indice]


def probabilita_esito(forecast_values: list[float], tipo: str, risoluzione: str, opzioni: list[str] | None, scaling: dict | None) -> tuple[float, str]:
    """(probabilità data all'esito, descrizione breve della previsione)."""
    if tipo == "binary":
        p_si = float(forecast_values[1]) if len(forecast_values) > 1 else float(forecast_values[0])
        esito = str(risoluzione).strip().lower()
        if esito not in {"yes", "no"}:
            raise ValueError(f"risoluzione binaria non valida: {risoluzione}")
        return (p_si if esito == "yes" else 1.0 - p_si), f"{p_si:.0%} sì"
    if tipo == "multiple_choice":
        if not opzioni or len(opzioni) != len(forecast_values):
            raise ValueError("opzioni e valori non combaciano")
        indice = next((i for i, o in enumerate(opzioni) if str(o).strip().lower() == str(risoluzione).strip().lower()), None)
        if indice is None:
            raise ValueError(f"opzione risolta sconosciuta: {risoluzione}")
        migliore = opzioni[max(range(len(opzioni)), key=lambda i: forecast_values[i])]
        return float(forecast_values[indice]), f"{forecast_values[indice]:.0%} su {risoluzione} (max: {migliore})"
    if tipo in {"numeric", "date", "discrete"}:
        if not scaling:
            raise ValueError("manca la scala della domanda")
        cdf = [float(v) for v in forecast_values]
        massa = massa_del_bin(cdf, risoluzione, "date" if tipo == "date" else "numeric", scaling)
        mediana = next((i for i, v in enumerate(cdf) if v >= 0.5), len(cdf) - 1) / max(1, len(cdf) - 1)
        return massa, f"{massa:.1%} sul bin dell'esito (mediana a {mediana:.0%} della scala)"
    raise ValueError(f"tipo non gestito: {tipo}")


def _storia_community(domanda: dict) -> list[dict]:
    aggregazioni = domanda.get("aggregations") or {}
    for nome in ("recency_weighted", "unweighted", "metaculus_prediction"):
        storia = (aggregazioni.get(nome) or {}).get("history")
        if storia:
            return storia
    return []


def _nome_torneo(post: dict) -> str:
    progetti = post.get("projects") or {}
    predefinito = progetti.get("default_project") or {}
    return str(predefinito.get("slug") or predefinito.get("name") or "?")


def valuta_domanda(post: dict, domanda: dict) -> RigaPagella | None:
    """
    Una riga di pagella da un post e una delle sue domande (JSON dell'API).
    None se manca qualcosa (non risolta, niente nostra previsione prima della
    chiusura, niente community prediction, risoluzione annullata).
    """
    risoluzione = domanda.get("resolution")
    if risoluzione in (None, "", "annulled", "ambiguous"):
        return None
    tipo = str(domanda.get("type") or "")
    chiusura_testo = domanda.get("actual_close_time") or domanda.get("scheduled_close_time")
    if not chiusura_testo:
        return None
    chiusura = datetime.fromisoformat(str(chiusura_testo).replace("Z", "+00:00")).timestamp()

    nostra = _ultimo_prima(((domanda.get("my_forecasts") or {}).get("history")) or [], chiusura)
    community = _ultimo_prima(_storia_community(domanda), chiusura)
    if nostra is None or community is None:
        return None
    valori_community = community.get("forecast_values")
    if not valori_community and tipo == "binary" and community.get("centers"):
        valori_community = [1.0 - float(community["centers"][0]), float(community["centers"][0])]
    if not valori_community:
        return None
    try:
        p_nostra, testo_nostra = probabilita_esito(
            nostra["forecast_values"], tipo, str(risoluzione), domanda.get("options"), domanda.get("scaling")
        )
        p_community, testo_community = probabilita_esito(
            valori_community, tipo, str(risoluzione), domanda.get("options"), domanda.get("scaling")
        )
    except (ValueError, KeyError, IndexError, TypeError) as errore:
        logger.warning(f"Pagella: domanda {domanda.get('id')} non valutabile: {errore}")
        return None
    log_nostro = _log(p_nostra)
    log_community = _log(p_community)
    return RigaPagella(
        domanda=f"{post.get('id')}-{domanda.get('id')}",
        post=int(post.get("id") or 0),
        titolo=str(domanda.get("title") or post.get("title") or "")[:160],
        torneo=_nome_torneo(post),
        tipo=tipo,
        chiusura=datetime.fromtimestamp(chiusura, tz=timezone.utc).isoformat(timespec="minutes"),
        risolto_il=domanda.get("actual_resolve_time"),
        risoluzione=str(risoluzione),
        nostra=testo_nostra,
        community=testo_community,
        log_nostro=log_nostro,
        log_community=log_community,
        peer_stimato=100.0 * (log_nostro - log_community),
    )


def valuta_post(post: dict) -> list[RigaPagella]:
    domande: list[dict] = []
    if post.get("question"):
        domande.append(post["question"])
    for sotto in ((post.get("group_of_questions") or {}).get("questions")) or []:
        domande.append(sotto)
    righe = []
    for domanda in domande:
        riga = valuta_domanda(post, domanda)
        if riga is not None:
            righe.append(riga)
    return righe


# ------------------------------------------------------------------ SQLite
def _apri(percorso: Path) -> sqlite3.Connection:
    percorso = Path(percorso)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(percorso))
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS pagella (
            domanda TEXT PRIMARY KEY,
            post INTEGER,
            titolo TEXT,
            torneo TEXT,
            tipo TEXT,
            chiusura TEXT,
            risolto_il TEXT,
            risoluzione TEXT,
            nostra TEXT,
            community TEXT,
            log_nostro REAL,
            log_community REAL,
            peer_stimato REAL,
            aggiornato TEXT NOT NULL
        )
        """
    )
    return db


def salva_righe(righe: list[RigaPagella], percorso: Path = PERCORSO_PAGELLA) -> int:
    db = _apri(percorso)
    try:
        adesso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for riga in righe:
            dati = asdict(riga)
            dati["aggiornato"] = adesso
            colonne = ", ".join(dati)
            segnaposto = ", ".join("?" for _ in dati)
            db.execute(f"INSERT OR REPLACE INTO pagella ({colonne}) VALUES ({segnaposto})", tuple(dati.values()))
        db.commit()
    finally:
        db.close()
    return len(righe)


def riepilogo(percorso: Path = PERCORSO_PAGELLA) -> dict:
    """Numeri per torneo e totali: domande, media peer stimato, quante volte battiamo la community."""
    if not Path(percorso).exists():
        return {"tornei": [], "totale": None}
    db = _apri(percorso)
    try:
        righe = db.execute(
            "SELECT torneo, COUNT(*), AVG(peer_stimato), SUM(peer_stimato), AVG(log_nostro), AVG(log_community),"
            " SUM(log_nostro > log_community) FROM pagella GROUP BY torneo ORDER BY torneo"
        ).fetchall()
        totale = db.execute(
            "SELECT COUNT(*), AVG(peer_stimato), SUM(peer_stimato), AVG(log_nostro), AVG(log_community),"
            " SUM(log_nostro > log_community) FROM pagella"
        ).fetchone()
    finally:
        db.close()
    tornei = [
        dict(torneo=t, n=n, peer_medio=pm, peer_totale=pt, log_nostro=ln, log_community=lc, vinte=v)
        for t, n, pm, pt, ln, lc, v in righe
    ]
    tot = None
    if totale and totale[0]:
        tot = dict(n=totale[0], peer_medio=totale[1], peer_totale=totale[2], log_nostro=totale[3], log_community=totale[4], vinte=totale[5])
    return {"tornei": tornei, "totale": tot}


def tabella_markdown(percorso: Path = PERCORSO_PAGELLA, limite: int = 15) -> list[str]:
    """Righe markdown per il rapporto: medie per torneo e le ultime domande risolte."""
    dati = riepilogo(percorso)
    if not dati["totale"]:
        return ["- Nessuna domanda risolta con una nostra previsione e una community prediction, per ora."]
    righe = []
    for t in dati["tornei"]:
        righe.append(
            f"- {t['torneo']}: {t['n']} domande, spot peer stimato medio {t['peer_medio']:+.1f} "
            f"(totale {t['peer_totale']:+.0f}), battiamo la community in {t['vinte']} su {t['n']}."
        )
    tot = dati["totale"]
    righe.append(
        f"- Totale: {tot['n']} domande, log score medio nostro {tot['log_nostro']:.3f} contro {tot['log_community']:.3f} "
        f"della community, peer stimato medio {tot['peer_medio']:+.1f}."
    )
    db = _apri(percorso)
    try:
        ultime = db.execute(
            "SELECT chiusura, torneo, tipo, titolo, risoluzione, nostra, community, peer_stimato"
            " FROM pagella ORDER BY chiusura DESC LIMIT ?",
            (limite,),
        ).fetchall()
    finally:
        db.close()
    righe += ["", "| Chiusura | Torneo | Tipo | Domanda | Esito | Noi | Community | Peer stimato |", "|---|---|---|---|---|---|---|---|"]
    for chiusura, torneo, tipo, titolo, esito, noi, community, peer in ultime:
        righe.append(
            f"| {chiusura[:10]} | {torneo} | {tipo} | {titolo[:60].replace('|', '/')} | {esito[:20]} | {noi} | {community} | {peer:+.1f} |"
        )
    righe.append("")
    righe.append(
        "Il peer score stimato usa la community prediction come 'altro previsore medio': è prudente, "
        "perché l'aggregato batte il membro medio. Il punteggio ufficiale è in classifica."
    )
    return righe


# ----------------------------------------------------------------- download
def _intestazioni(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Accept-Language": "en"}


def _get(percorso: str, token: str, **parametri) -> dict:
    risposta = requests.get(f"{API}{percorso}", headers=_intestazioni(token), params=parametri, timeout=TIMEOUT)
    risposta.raise_for_status()
    return risposta.json()


def scarica_post_risolti(token: str, massimo: int = 500) -> list[dict]:
    """I post risolti su cui questo bot ha previsto, con community prediction e nostra storia."""
    me = _get("/users/me/", token)
    mio_id = me.get("id")
    if not mio_id:
        raise RuntimeError("utente del token non leggibile")
    post: list[dict] = []
    offset = 0
    while len(post) < massimo:
        pagina = _get(
            "/posts/", token,
            forecaster_id=mio_id, statuses="resolved", with_cp="true", limit=100, offset=offset, order_by="-scheduled_close_time",
        )
        risultati = pagina.get("results") or []
        post += [p for p in risultati if "notebook" not in p]
        if len(risultati) < 100:
            break
        offset += 100
    return post


def aggiorna_pagella(token: str, percorso: Path = PERCORSO_PAGELLA) -> int:
    """Scarica, valuta e salva. Restituisce il numero di righe scritte (0 se qualcosa va storto)."""
    try:
        posts = scarica_post_risolti(token)
    except Exception as errore:
        logger.warning(f"Pagella: download fallito: {type(errore).__name__}: {errore}")
        return 0
    righe: list[RigaPagella] = []
    for p in posts:
        try:
            righe += valuta_post(p)
        except Exception as errore:  # un post strano non deve fermare gli altri
            logger.warning(f"Pagella: post {p.get('id')} saltato: {type(errore).__name__}: {errore}")
    if not righe:
        logger.info(f"Pagella: {len(posts)} post risolti scaricati, nessuno valutabile.")
        return 0
    return salva_righe(righe, percorso)


def righe_json(righe: list[RigaPagella]) -> str:
    return json.dumps([asdict(r) for r in righe], ensure_ascii=False, indent=1)
