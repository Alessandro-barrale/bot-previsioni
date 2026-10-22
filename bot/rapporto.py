"""
Rapporto settimanale in italiano, scritto in rapporti/AAAA-MM-GG.md.

Dati locali (sempre): domande coperte e saltate, spesa della settimana e del mese,
pagella (nostre previsioni contro la community, da dati/pagella.sqlite).
Dati Metaculus (se l'API li espone al token del bot): posizione e punteggio in
classifica, premio stimato. Se l'API non risponde, il rapporto lo dice e va avanti.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from bot.config import CARTELLA_PROGETTO, TORNEO_MARKET_PULSE_PREDEFINITO, Impostazioni, chiave_presente
from bot.spese import Registro

logger = logging.getLogger(__name__)

API = "https://www.metaculus.com/api"
# Montepremi (dollari): torneo stagionale Fall 2026 e ogni MiniBench.
TORNEI = {
    "stagione": ("fall-futureeval-2026", 50_000),
    "minibench": ("minibench", 1_000),
    # In classifica solo se il bot ha già previsto lì (registro con torneo market_pulse).
    "market_pulse": (os.getenv("TORNEO_MARKET_PULSE") or TORNEO_MARKET_PULSE_PREDEFINITO, 7_500),
}
PREMIO_MINIMO_PAGATO = 50


def _intestazioni() -> dict:
    return {"Authorization": f"Token {os.environ['METACULUS_TOKEN']}", "Accept-Language": "en"}


def _get(percorso: str, **parametri) -> dict | list:
    risposta = requests.get(f"{API}{percorso}", headers=_intestazioni(), params=parametri, timeout=30)
    risposta.raise_for_status()
    return risposta.json()


def _mio_nome_utente() -> str | None:
    try:
        dati = _get("/users/me/")
        return dati.get("username") if isinstance(dati, dict) else None
    except Exception as errore:
        logger.warning(f"Utente del bot non leggibile: {errore}")
        return None


def stima_premio(mio_punteggio: float, punteggi: list[float], montepremi: float) -> float:
    """
    Stima non ufficiale riportata dai partecipanti: premio proporzionale al
    quadrato del punteggio positivo. Sotto i 50 $ non si paga.
    """
    if mio_punteggio <= 0:
        return 0.0
    totale = sum(max(0.0, p) ** 2 for p in punteggi)
    if totale <= 0:
        return 0.0
    premio = montepremi * mio_punteggio**2 / totale
    return premio if premio >= PREMIO_MINIMO_PAGATO else 0.0


def classifica(torneo: str, nome_utente: str | None) -> str:
    slug, montepremi = TORNEI[torneo]
    try:
        dati = _get(f"/leaderboards/project/{slug}/")
    except Exception as errore:
        return f"- {torneo}: classifica non disponibile dall'API ({type(errore).__name__})."
    voci = dati.get("entries") if isinstance(dati, dict) else dati
    if not isinstance(voci, list) or not voci:
        return f"- {torneo}: classifica ancora vuota."

    def nome(voce: dict) -> str | None:
        utente = voce.get("user") or {}
        return utente.get("username") if isinstance(utente, dict) else None

    punteggi = [float(v.get("score") or 0) for v in voci]
    mia = next((v for v in voci if nome_utente and nome(v) == nome_utente), None)
    if mia is None:
        return f"- {torneo}: il bot non è ancora in classifica ({len(voci)} partecipanti)."
    punteggio = float(mia.get("score") or 0)
    premio_ufficiale = mia.get("prize")
    if premio_ufficiale is not None:
        premio = f"{float(premio_ufficiale):.0f} $ (dato Metaculus)"
    else:
        premio = f"circa {stima_premio(punteggio, punteggi, montepremi):.0f} $ (stima)"
    return (
        f"- {torneo}: posizione {mia.get('rank', '?')} su {len(voci)}, punteggio {punteggio:.1f}, "
        f"premio {premio}."
    )


def scrivi_rapporto(
    imp: Impostazioni, adesso: datetime | None = None, cartella: Path | None = None, percorso_pagella: Path | None = None
) -> Path:
    adesso = adesso or datetime.now(timezone.utc)
    cartella = cartella or CARTELLA_PROGETTO / "rapporti"
    cartella.mkdir(parents=True, exist_ok=True)
    inizio = (adesso - timedelta(days=7)).isoformat(timespec="seconds")
    mese = adesso.strftime("%Y-%m")

    registro = Registro(imp.percorso_registro, imp.tetto_domanda, imp.tetto_giorno, imp.tetto_mese)
    try:
        per_torneo = registro.interroga(
            "SELECT COALESCE(torneo,'?'), SUM(errore IS NULL), SUM(errore IS NOT NULL), SUM(inviata)"
            " FROM previsioni WHERE momento >= ? GROUP BY torneo ORDER BY torneo",
            (inizio,),
        )
        spesa_settimana = registro.interroga(
            "SELECT COALESCE(SUM(costo),0), COUNT(*) FROM chiamate WHERE momento >= ?", (inizio,)
        )[0]
        spesa_mese = registro.spesa_mese(adesso)
        media = registro.interroga(
            "SELECT AVG(costo) FROM previsioni WHERE errore IS NULL AND momento >= ?", (inizio,)
        )[0][0]
        errori = registro.interroga(
            "SELECT errore, COUNT(*) FROM previsioni WHERE errore IS NOT NULL AND momento >= ?"
            " GROUP BY substr(errore,1,60) ORDER BY 2 DESC LIMIT 3",
            (inizio,),
        )
        market_pulse = registro.interroga("SELECT COUNT(*) FROM previsioni WHERE torneo = 'market_pulse'")[0][0] > 0
        spesa_mp = registro.spesa_mese_torneo("market_pulse", adesso)
    finally:
        registro.chiudi()

    righe = [f"# Rapporto settimanale del bot – {adesso.strftime('%d/%m/%Y')}", ""]
    righe.append(f"Periodo: ultimi 7 giorni. Invio a Metaculus: {'ACCESO' if imp.invia_previsioni else 'spento (solo prova)'}.")
    righe += ["", "## Domande"]
    if per_torneo:
        for torneo, fatte, saltate, inviate in per_torneo:
            righe.append(f"- {torneo}: {fatte} previste ({inviate} inviate), {saltate} saltate.")
    else:
        righe.append("- Nessuna domanda nuova questa settimana.")
    if errori:
        righe.append("- Motivi più frequenti dei salti: " + "; ".join(f"{e[:60]} ({n})" for e, n in errori))

    righe += ["", "## Spesa"]
    righe.append(f"- Settimana: {spesa_settimana[0]:.2f} $ in {spesa_settimana[1]} chiamate.")
    righe.append(f"- Mese {mese}: {spesa_mese:.2f} $ su un tetto di {imp.tetto_mese:.0f} $.")
    if media is not None:
        righe.append(f"- Media per domanda: {media:.2f} $ (obiettivo {imp.obiettivo_medio_domanda:.2f} $).")
    if market_pulse:
        righe.append(f"- Market Pulse, mese {mese}: {spesa_mp:.2f} $ su un tetto proprio di {imp.tetto_mese_mp:.0f} $.")

    righe += ["", "## Pagella: noi contro la community (domande risolte)"]
    try:
        from bot import pagella

        percorso_pagella = percorso_pagella or Path(os.getenv("PERCORSO_PAGELLA") or pagella.PERCORSO_PAGELLA)
        righe += pagella.tabella_markdown(percorso_pagella)
    except Exception as errore:  # la pagella non deve mai bloccare il rapporto
        righe.append(f"- Pagella non disponibile ({type(errore).__name__}).")

    righe += ["", "## Classifica e premi"]
    if chiave_presente("METACULUS_TOKEN"):
        nome_utente = _mio_nome_utente()
        for torneo in TORNEI:
            if torneo == "market_pulse" and not market_pulse:
                continue
            righe.append(classifica(torneo, nome_utente))
        righe.append(
            "- I punteggi arrivano solo quando le domande si chiudono e si risolvono: nelle prime settimane è normale vederli vuoti."
        )
    else:
        righe.append("- Manca METACULUS_TOKEN: classifica non consultata.")

    percorso = cartella / f"{adesso.strftime('%Y-%m-%d')}.md"
    percorso.write_text("\n".join(righe) + "\n", encoding="utf-8")
    return percorso
