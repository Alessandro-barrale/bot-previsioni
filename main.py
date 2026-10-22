"""
Avvio del bot.

    uv run python main.py --modo prova_offline        # 4 domande finte, nessun invio
    uv run python main.py --modo test_questions       # domande vere dell'area di prova (serve METACULUS_TOKEN)
    uv run python main.py --modo torneo               # torneo stagionale + MiniBench
    uv run python main.py --modo rapporto             # rapporto settimanale in rapporti/
    uv run python main.py --modo pagella              # pagella: noi contro la community (serve METACULUS_TOKEN)
    uv run python main.py --modo torneo --bot 2       # bot secondario (METACULUS_TOKEN_2, registro separato)

L'invio a Metaculus parte solo se INVIA_PREVISIONI=si (variabile del repository
in GitHub, oppure .env in locale). Con il modo prova_offline non si invia mai.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import dotenv

from bot_helpers import check_environment, print_run_summary_banner, print_startup_banner, silence_noisy_dependencies

silence_noisy_dependencies()
dotenv.load_dotenv()

from forecasting_tools import MetaculusClient  # noqa: E402

from bot.config import Impostazioni  # noqa: E402
from bot.llm import crea_llm  # noqa: E402
from bot.previsore import BotPrevisioni, chiave_domanda  # noqa: E402
from bot.ricerca import Ricercatore  # noqa: E402
from bot.spese import Registro  # noqa: E402

logger = logging.getLogger(__name__)

URL_TORNEI = {
    "torneo": "https://www.metaculus.com/tournament/fall-futureeval-2026/",
    "test_questions": "https://www.metaculus.com/tournament/bot-testing-area/",
}


def _fonti_finte(registro: Registro, imp: Impostazioni) -> dict:
    """
    Fonti finte per la prova offline. Restituiscono testi lunghi quanto quelli
    veri (circa 3.000 token ciascuno) e registrano una chiamata simulata con i
    token e le ricerche di una ricerca web reale, così il costo stimato per
    domanda è realistico.
    """
    voce = "- (finto) Articolo di agenzia pubblicato il 2026-10-{giorno:02d}: dettagli, cifre e citazioni di esperti sul tema della domanda. "

    def testo(n: int) -> str:
        return "\n".join(voce.format(giorno=1 + i % 13) * 3 for i in range(n))

    async def web(richiesta: str) -> str:
        # Ricerca web reale tipica: ~15.000 token letti, ~1.500 scritti, 4 ricerche.
        registro.registra_chiamata(imp.modello_ricerca, 15000, 1500, ricerche_web=imp.ricerche_web_per_giro, scopo="ricerca_web (simulata)")
        return testo(30)

    async def notizie(domanda: str) -> str:
        registro.registra_chiamata("asknews/news", 0, 0, costo=0.0, ricerche_web=2, scopo="asknews")
        return testo(30)

    return {"cerca_web": web, "cerca_notizie": notizie}


async def _fonte_guasta(_: str) -> str:
    raise ConnectionError("fonte simulata non raggiungibile")


def costruisci_bot(imp: Impostazioni, registro: Registro, torneo: str | None, prova_offline: bool, guasto: bool, rifai: bool) -> BotPrevisioni:
    finto = imp.modello_finto
    modelli = [crea_llm(m, registro, "previsione", finto) for m in imp.modelli_previsione]
    ricerca = crea_llm(imp.modello_ricerca, registro, "ricerca", finto)
    riassunto = crea_llm(imp.modello_riassunto, registro, "riassunto", finto)
    fonti = {}
    if prova_offline and finto:
        fonti = _fonti_finte(registro, imp)
    if guasto:
        fonti["cerca_notizie"] = _fonte_guasta
    ricercatore = Ricercatore(imp, registro, ricerca, riassunto, **fonti)
    return BotPrevisioni(
        impostazioni=imp,
        registro=registro,
        modelli_previsione=modelli,
        modello_ricerca=ricerca,
        modello_riassunto=riassunto,
        ricercatore=ricercatore,
        torneo=torneo,
        rifai_domande_gia_previste=rifai,
    )


def stampa_costi(registro: Registro, domande) -> None:
    print("\nCosto per domanda (dal registro):")
    for d in domande:
        chiave = chiave_domanda(d)
        righe = registro.interroga(
            "SELECT COUNT(*), COALESCE(SUM(token_ingresso),0), COALESCE(SUM(token_uscita),0), COALESCE(SUM(costo),0)"
            " FROM chiamate WHERE domanda = ?",
            (chiave,),
        )[0]
        print(f"  {d.page_url}: {righe[0]} chiamate, {righe[1]} token in, {righe[2]} token out, {righe[3]:.3f} $")


def configura_bot_secondario() -> bool:
    """
    Il bot secondario usa il token METACULUS_TOKEN_2 (che diventa METACULUS_TOKEN
    per la libreria) e registri separati, così la v2 può girare in parallelo alla
    primaria senza mescolare previsioni e spese. Vero se il token c'è.
    """
    from bot.config import CARTELLA_PROGETTO, chiave_presente

    if not chiave_presente("METACULUS_TOKEN_2"):
        return False
    os.environ["METACULUS_TOKEN"] = os.environ["METACULUS_TOKEN_2"]
    os.environ.setdefault("PERCORSO_REGISTRO", str(CARTELLA_PROGETTO / "dati" / "registro_v2.sqlite"))
    os.environ.setdefault("PERCORSO_PAGELLA", str(CARTELLA_PROGETTO / "dati" / "pagella_v2.sqlite"))
    return True


def main() -> int:
    # Su Windows la console non è UTF-8: le emoji del template la farebbero cadere.
    for flusso in (sys.stdout, sys.stderr):
        if hasattr(flusso, "reconfigure"):
            flusso.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    parser = argparse.ArgumentParser(description="Bot di previsioni per Metaculus FutureEval")
    parser.add_argument(
        "--modo",
        choices=["torneo", "stagione", "minibench", "test_questions", "prova_offline", "rapporto", "pagella"],
        default="torneo",
    )
    parser.add_argument("--guasto", action="store_true", help="prova: simula una fonte di ricerca che non risponde")
    parser.add_argument(
        "--bot", type=int, choices=[1, 2], default=1,
        help="2 = bot secondario (METACULUS_TOKEN_2, registro dati/registro_v2.sqlite), per i test A/B",
    )
    args = parser.parse_args()
    if args.bot == 2 and not configura_bot_secondario():
        print("Manca METACULUS_TOKEN_2: il bot secondario non parte.")
        return 1

    if args.modo == "rapporto":
        from bot.rapporto import scrivi_rapporto

        imp = Impostazioni.da_ambiente()
        percorso = scrivi_rapporto(imp)
        print(f"Rapporto scritto in {percorso}")
        return 0

    if args.modo == "pagella":
        from bot import pagella
        from bot.config import chiave_presente

        if not chiave_presente("METACULUS_TOKEN"):
            print("Manca METACULUS_TOKEN: pagella non aggiornata (i test usano dati finti).")
            return 0
        percorso = Path(os.getenv("PERCORSO_PAGELLA") or pagella.PERCORSO_PAGELLA)
        scritte = pagella.aggiorna_pagella(os.environ["METACULUS_TOKEN"], percorso)
        print(f"Pagella: {scritte} domande risolte valutate, salvate in {percorso}")
        for riga in pagella.tabella_markdown(percorso):
            print(riga)
        return 0

    prova_offline = args.modo == "prova_offline"
    imp = Impostazioni.da_ambiente()
    if prova_offline:
        imp.invia_previsioni = False  # mai invio in prova
    else:
        check_environment(strict=True)
    print_startup_banner(args.modo, will_publish=imp.invia_previsioni)
    print(f"Modelli di previsione: {', '.join(imp.modelli_previsione)}")
    print(f"Tetti: {imp.tetto_domanda:.2f} $/domanda, {imp.tetto_giorno:.2f} $/giorno, {imp.tetto_mese:.2f} $/mese\n")

    registro = Registro(imp.percorso_registro, imp.tetto_domanda, imp.tetto_giorno, imp.tetto_mese)
    rapporti: list = []
    domande_fatte: list = []
    try:
        if prova_offline:
            from bot.domande_prova import domande_di_prova

            bot = costruisci_bot(imp, registro, "prova_offline", True, args.guasto, rifai=True)
            domande = domande_di_prova()
            rapporti = asyncio.run(bot.forecast_questions(domande, return_exceptions=True))
            domande_fatte = domande
        else:
            client = MetaculusClient()
            obiettivi = {
                "torneo": [("stagione", client.CURRENT_AI_COMPETITION_ID), ("minibench", client.CURRENT_MINIBENCH_ID)],
                "stagione": [("stagione", client.CURRENT_AI_COMPETITION_ID)],
                "minibench": [("minibench", client.CURRENT_MINIBENCH_ID)],
                "test_questions": [("bot-testing-area", "bot-testing-area")],
            }[args.modo]
            for nome, torneo_id in obiettivi:
                bot = costruisci_bot(imp, registro, nome, False, args.guasto, rifai=False)
                if args.modo == "test_questions":
                    bot.skip_previously_forecasted_questions = False
                domande = client.get_all_open_questions_from_tournament(torneo_id)
                rapporti += asyncio.run(bot.forecast_questions(domande, return_exceptions=True))
                domande_fatte += domande
        if args.modo in {"prova_offline", "test_questions"}:
            stampa_costi(registro, domande_fatte)
    finally:
        registro.chiudi()

    print_run_summary_banner(rapporti, will_publish=imp.invia_previsioni, tournament_url=URL_TORNEI.get(args.modo))
    # Le domande saltate (errori, tetti) non devono far fallire il giro: il bot riprova al giro dopo.
    # Se però falliscono tutte, il giro è rosso: di solito è una chiave sbagliata, e GitHub manda l'email.
    # Un tetto di spesa raggiunto invece è previsto e resta verde.
    errori = [r for r in rapporti if isinstance(r, BaseException)]
    benigni = ("tetto", "ChiusuraImminente", "saltata")
    if rapporti and len(errori) == len(rapporti) and not any(b in str(e) or b in type(e).__name__ for e in errori for b in benigni):
        print("Tutte le domande sono fallite: controlla le chiavi e il log.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
