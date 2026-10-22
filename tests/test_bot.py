"""Prove del bot completo con il modello finto: nessuna rete, nessun invio."""
import os

import pytest
from forecasting_tools import BinaryQuestion

from bot.config import Impostazioni
from bot.domande_prova import domande_di_prova
from bot.llm import LlmFinto
from bot.previsore import BotPrevisioni
from bot.ricerca import Ricercatore
from bot.spese import Registro


async def web_ok(richiesta):
    return "- Fonte web finta, 2026-10-10: niente di nuovo."


async def notizie_ok(domanda):
    return "**Notizia finta**\nPublish date: October 12, 2026"


async def guasta(_):
    raise ConnectionError("fonte giù")


class LlmRotto(LlmFinto):
    async def _chiama(self, prompt):
        raise RuntimeError("modello non disponibile")


def costruisci(tmp_path, cerca_web=web_ok, cerca_notizie=notizie_ok, rotto=False, tetto_domanda=5.0, tetto_mese=100.0):
    imp = Impostazioni(modello_finto=True, modelli_previsione=["finto/claude-opus-5-5", "finto/claude-sonnet-5-5"],
                       modello_ricerca="finto/claude-sonnet-5-5", modello_riassunto="finto/claude-haiku-5-5",
                       tetto_domanda=tetto_domanda, tetto_mese=tetto_mese, percorso_registro=tmp_path / "r.sqlite")
    registro = Registro(imp.percorso_registro, imp.tetto_domanda, imp.tetto_giorno, imp.tetto_mese)
    classe = LlmRotto if rotto else LlmFinto
    modelli = [classe(model=m, registro=registro, scopo="previsione", seme=i) for i, m in enumerate(imp.modelli_previsione)]
    ricerca = LlmFinto(model=imp.modello_ricerca, registro=registro, scopo="ricerca")
    riassunto = LlmFinto(model=imp.modello_riassunto, registro=registro, scopo="riassunto")
    ricercatore = Ricercatore(imp, registro, ricerca, riassunto, cerca_web=cerca_web, cerca_notizie=cerca_notizie)
    bot = BotPrevisioni(impostazioni=imp, registro=registro, modelli_previsione=modelli, modello_ricerca=ricerca,
                        modello_riassunto=riassunto, ricercatore=ricercatore, torneo="prova")
    return bot, registro


async def test_ogni_tipo_di_domanda(tmp_path):
    bot, registro = costruisci(tmp_path)
    assert bot.publish_reports_to_metaculus is False
    rapporti = await bot.forecast_questions(domande_di_prova(), return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in rapporti), rapporti
    binaria = rapporti[0]
    assert 0.02 <= binaria.prediction <= 0.98
    righe = registro.interroga("SELECT tipo, errore FROM previsioni ORDER BY id")
    assert [r[0] for r in righe] == ["binary", "numeric", "multiple_choice", "date"]
    assert all(r[1] is None for r in righe)


async def test_rotazione_su_due_modelli(tmp_path):
    bot, registro = costruisci(tmp_path)
    await bot.forecast_questions(domande_di_prova()[:1], return_exceptions=True)
    conteggi = dict(registro.interroga(
        "SELECT modello, COUNT(*) FROM chiamate WHERE scopo='previsione' GROUP BY modello"))
    assert conteggi == {"finto/claude-opus-5-5": 3, "finto/claude-sonnet-5-5": 2}


async def test_mediana_delle_cinque(tmp_path):
    bot, _ = costruisci(tmp_path)
    domanda = domande_di_prova()[0]
    previsioni = [0.1, 0.5, 0.2, 0.9, 0.3]
    assert await bot._aggregate_predictions(previsioni, domanda) == pytest.approx(0.3)


async def test_una_fonte_guasta_non_blocca(tmp_path):
    bot, _ = costruisci(tmp_path, cerca_notizie=guasta)
    domanda = domande_di_prova()[0]
    ricerca = await bot.ricercatore.ricerca(domanda)
    assert "AskNews (ConnectionError)" in ricerca
    assert "Source: web" in ricerca
    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException)


async def test_tutte_le_fonti_guaste_usa_il_modello(tmp_path):
    bot, _ = costruisci(tmp_path, cerca_web=guasta, cerca_notizie=guasta)
    ricerca = await bot.ricercatore.ricerca(domande_di_prova()[0])
    assert "model background knowledge" in ricerca


async def test_modello_guasto_salta_la_domanda(tmp_path):
    bot, registro = costruisci(tmp_path, rotto=True)
    rapporti = await bot.forecast_questions(domande_di_prova()[:2], return_exceptions=True)
    assert all(isinstance(r, BaseException) for r in rapporti)
    errori = registro.interroga("SELECT errore FROM previsioni")
    assert len(errori) == 2 and all(e[0] for e in errori)


async def test_tetto_mensile_salta_senza_spendere(tmp_path):
    bot, registro = costruisci(tmp_path, tetto_mese=0.0)
    rapporti = await bot.forecast_questions(domande_di_prova()[:1], return_exceptions=True)
    assert isinstance(rapporti[0], BaseException)
    assert registro.interroga("SELECT COUNT(*) FROM chiamate WHERE costo > 0")[0][0] == 0


async def test_tetto_per_domanda_ferma_la_domanda(tmp_path):
    bot, registro = costruisci(tmp_path, tetto_domanda=0.05)
    rapporti = await bot.forecast_questions(domande_di_prova()[:1], return_exceptions=True)
    assert isinstance(rapporti[0], BaseException)
    # Le chiamate partite in parallelo possono sforare di poco, mai di molto.
    assert registro.spesa_domanda("-1--1") < 0.05 + 6 * 0.08


async def test_domanda_gia_prevista_non_si_rifa(tmp_path):
    bot, registro = costruisci(tmp_path)
    domanda = domande_di_prova()[0]
    await bot.forecast_questions([domanda], return_exceptions=True)
    chiamate = registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0]
    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)
    assert rapporti == []
    assert registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0] == chiamate


def test_invio_spento_per_impostazione():
    assert Impostazioni().invia_previsioni is False
    assert isinstance(domande_di_prova()[0], BinaryQuestion)


class LlmCheRegistraPrompt(LlmFinto):
    prompt_visti: list[str] = []

    async def _chiama(self, prompt):
        LlmCheRegistraPrompt.prompt_visti.append(prompt if isinstance(prompt, str) else str(prompt))
        return await super()._chiama(prompt)


async def test_alternanza_outside_inside_e_blocco_json(tmp_path):
    LlmCheRegistraPrompt.prompt_visti = []
    bot, registro = costruisci(tmp_path)
    bot.modelli_previsione = [
        LlmCheRegistraPrompt(model=m.model, registro=registro, scopo="previsione", seme=i)
        for i, m in enumerate(bot.modelli_previsione)
    ]
    await bot.forecast_questions(domande_di_prova()[:1], return_exceptions=True)
    visti = LlmCheRegistraPrompt.prompt_visti
    assert len(visti) == 5
    outside = [p for p in visti if "lens for this sample: OUTSIDE VIEW" in p]
    inside = [p for p in visti if "lens for this sample: INSIDE VIEW" in p]
    assert len(outside) == 3 and len(inside) == 2
    for p in visti:
        assert "STATUS QUO FIRST" in p and "RESOLUTION CHECK" in p and "ANCHOR ON YOUR MATH" in p
        assert "SOFT CLOCK" in p and "HISTORY DISCHARGED" in p and "NULL RESULT" in p
        assert "Boolean product" in p and "9:1 odds" in p
        assert '{"probability": 0.37}' in p
    assert [BotPrevisioni.angolo(t) for t in (1, 2, 3, 4, 5, 6)] == ["outside", "inside"] * 3


async def test_prompt_numerico_chiede_13_percentili_e_scoring(tmp_path):
    LlmCheRegistraPrompt.prompt_visti = []
    bot, registro = costruisci(tmp_path)
    bot.modelli_previsione = [LlmCheRegistraPrompt(model="finto/claude-opus-5-5", registro=registro, scopo="previsione", seme=0)]
    domande = domande_di_prova()
    rapporti = await bot.forecast_questions([domande[1], domande[3]], return_exceptions=True)
    assert all(not isinstance(r, BaseException) for r in rapporti), rapporti
    numerici = [p for p in LlmCheRegistraPrompt.prompt_visti if "Question type: numeric" in p]
    date = [p for p in LlmCheRegistraPrompt.prompt_visti if "Question type: date" in p]
    assert numerici and date
    assert "log density score" in numerici[0] and "random walk" in numerici[0] and '"97.5": 0' in numerici[0]
    assert "status quo is that it does NOT happen" in date[0] and '"2.5": "2027-03-15"' in date[0]
    previsione = registro.interroga("SELECT previsione FROM previsioni WHERE tipo='numeric'")[0][0]
    assert '"p2.5"' in previsione and '"p97.5"' in previsione


# ------------------------------------------------------------ ricerca v2
async def fonte_ok(domanda):
    return "What the resolution source says NOW: page text (2026-10-20)"


async def mercati_ok(domanda):
    return "Prediction markets: - [Polymarket] x 40% yes, weak"


async def serie_ok(domanda):
    return "Historical data series: latest 100"


def costruisci_v2(tmp_path, **fonti):
    bot, registro = costruisci(tmp_path)
    r = bot.ricercatore
    bot.ricercatore = Ricercatore(
        r.imp, registro, r.modello_ricerca, r.modello_riassunto, cerca_web=web_ok, cerca_notizie=notizie_ok,
        fonte_risoluzione=fonti.get("fonte_risoluzione", fonte_ok), mercati=fonti.get("mercati", mercati_ok),
        serie_storiche=fonti.get("serie_storiche", serie_ok),
    )
    return bot, registro


async def test_ricerca_v2_ordine_delle_sezioni_e_serie_solo_numeriche(tmp_path):
    bot, _ = costruisci_v2(tmp_path)
    binaria, numerica = domande_di_prova()[0], domande_di_prova()[1]
    testo = await bot.ricercatore.ricerca(binaria)
    posizioni = [testo.index(s) for s in ("Source: resolution source", "Source: prediction markets", "Source: AskNews", "Source: web (round 1)", "Source: web (round 2")]
    assert posizioni == sorted(posizioni)
    assert "Source: data series" not in testo
    testo = await bot.ricercatore.ricerca(numerica)
    assert "Source: data series" in testo


async def test_ricerca_v2_fonte_nuova_guasta_non_blocca(tmp_path):
    bot, _ = costruisci_v2(tmp_path, mercati=guasta, fonte_risoluzione=guasta)
    testo = await bot.ricercatore.ricerca(domande_di_prova()[0])
    assert "prediction markets (ConnectionError)" in testo and "resolution source (ConnectionError)" in testo
    assert "Source: web (round 1)" in testo
    rapporti = await bot.forecast_questions(domande_di_prova()[:1], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException)


async def test_via_veloce_solo_web_e_mercati(tmp_path):
    bot, registro = costruisci_v2(tmp_path)
    testo = await bot.ricercatore.ricerca(domande_di_prova()[1], veloce=True)
    assert "FAST research path" in testo
    assert "Source: prediction markets" in testo and "Source: web (round 1)" in testo
    assert "resolution source" not in testo and "data series" not in testo and "AskNews" not in testo
    assert "round 2" not in testo
    assert registro.interroga("SELECT COUNT(*) FROM chiamate WHERE scopo='riassunto'")[0][0] == 0


async def test_tempo_alla_chiusura_salto_e_via_veloce(tmp_path):
    from datetime import datetime, timedelta, timezone

    from bot.previsore import ChiusuraImminente, minuti_alla_chiusura

    bot, registro = costruisci_v2(tmp_path)
    domanda = domande_di_prova()[0]
    domanda.close_time = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert 4 < minuti_alla_chiusura(domanda) <= 5
    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)
    assert isinstance(rapporti[0], BaseException) and "ChiusuraImminente" in repr(rapporti[0]) + type(rapporti[0]).__name__ + str(rapporti[0])
    assert registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0] == 0
    errore = registro.interroga("SELECT errore FROM previsioni")[0][0]
    assert "ChiusuraImminente" in errore

    domanda.close_time = datetime.now(timezone.utc) + timedelta(minutes=30)
    testo = await bot.run_research(domanda)
    assert "FAST research path" in testo

    domanda.close_time = None
    assert minuti_alla_chiusura(domanda) is None
    testo = await bot.run_research(domanda)
    assert "FAST research path" not in testo


def test_interruttori_e_tetti_v2(monkeypatch):
    for nome in ("FONTE_RISOLUZIONE", "MERCATI", "SERIE_STORICHE", "TETTO_DOMANDA_USD", "TETTO_MESE_USD"):
        monkeypatch.delenv(nome, raising=False)
    imp = Impostazioni.da_ambiente()
    assert imp.fonte_risoluzione and imp.mercati and imp.serie_storiche
    assert imp.tetto_domanda == 0.90 and imp.tetto_mese == 150.0 and imp.obiettivo_medio_domanda == 0.50
    assert imp.minuti_salto == 12 and imp.minuti_via_veloce == 40
    monkeypatch.setenv("MERCATI", "no")
    monkeypatch.setenv("MODELLO_FINTO", "no")
    imp = Impostazioni.da_ambiente()
    assert imp.mercati is False and imp.fonte_risoluzione is True
    r = Ricercatore(imp, Registro(":memory:", 1, 1, 1) if False else None, None, None, cerca_web=web_ok, cerca_notizie=notizie_ok) if False else None


# ------------------------------------------------------------ roster a 3 vendor
def test_roster_tre_vendor_con_openrouter(monkeypatch):
    from bot.llm import parametri_modello

    for nome in ("MODELLI_PREVISIONE", "PREVISIONI_PER_DOMANDA", "MODELLO_FINTO", "ROSTER_3_VENDOR", "MODELLO_OPENAI",
                 "MODELLO_GOOGLE", "EFFORT_PREVISIONE", "EFFORT_ANTHROPIC"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chiave-finta")
    monkeypatch.setenv("OPENROUTER_API_KEY", "chiave-finta")
    imp = Impostazioni.da_ambiente()
    assert imp.modelli_previsione == [
        "anthropic/claude-opus-5-5", "openrouter/openai/gpt-5.6-sol", "openrouter/google/gemini-3.7-flash",
    ]
    assert imp.previsioni_per_domanda == 6
    assert parametri_modello("openrouter/openai/gpt-5.6-sol", "previsione") == {"reasoning_effort": "high"}
    assert parametri_modello("openrouter/google/gemini-3.7-flash", "previsione") == {"reasoning_effort": "high"}
    assert parametri_modello("anthropic/claude-opus-5-5", "previsione") == {}
    assert parametri_modello("openrouter/openai/gpt-5.6-sol", "riassunto") == {}
    monkeypatch.setenv("EFFORT_ANTHROPIC", "high")
    assert parametri_modello("openrouter/anthropic/claude-opus-5.5", "previsione") == {"reasoning_effort": "high"}

    monkeypatch.setenv("MODELLO_GOOGLE", "google/gemini-4-argon")
    monkeypatch.setenv("ROSTER_3_VENDOR", "no")
    imp = Impostazioni.da_ambiente()
    assert imp.modelli_previsione == ["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5"]
    assert imp.previsioni_per_domanda == 5
    monkeypatch.delenv("ROSTER_3_VENDOR")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    imp = Impostazioni.da_ambiente()
    assert imp.modelli_previsione[0] == "openrouter/anthropic/claude-opus-5.5"
    assert imp.modelli_previsione[2] == "openrouter/google/gemini-4-argon"


def test_crea_llm_senza_verbosity_con_effort(monkeypatch, tmp_path):
    from bot.llm import crea_llm

    monkeypatch.delenv("EFFORT_PREVISIONE", raising=False)
    monkeypatch.delenv("EFFORT_ANTHROPIC", raising=False)
    registro = Registro(tmp_path / "r.sqlite", 1, 1, 1)
    llm = crea_llm("openrouter/anthropic/claude-opus-5.5", registro, "previsione", finto=False, verbosity="high")
    assert "verbosity" not in llm.litellm_kwargs and "reasoning_effort" not in llm.litellm_kwargs
    llm = crea_llm("openrouter/openai/gpt-5.6-sol", registro, "previsione", finto=False)
    assert llm.litellm_kwargs["reasoning_effort"] == "high" and "verbosity" not in llm.litellm_kwargs
    assert llm.litellm_kwargs["temperature"] is None
    registro.chiudi()


async def test_rotazione_su_tre_modelli_sei_campioni(tmp_path):
    LlmCheRegistraPrompt.prompt_visti = []
    imp = Impostazioni(modello_finto=True, modelli_previsione=["finto/a", "finto/b", "finto/c"], previsioni_per_domanda=6,
                       modello_ricerca="finto/r", modello_riassunto="finto/s", percorso_registro=tmp_path / "r.sqlite")
    registro = Registro(imp.percorso_registro, imp.tetto_domanda, imp.tetto_giorno, imp.tetto_mese)
    modelli = [LlmCheRegistraPrompt(model=m, registro=registro, scopo="previsione", seme=i) for i, m in enumerate(imp.modelli_previsione)]
    ricerca = LlmFinto(model=imp.modello_ricerca, registro=registro, scopo="ricerca")
    riassunto = LlmFinto(model=imp.modello_riassunto, registro=registro, scopo="riassunto")
    ricercatore = Ricercatore(imp, registro, ricerca, riassunto, cerca_web=web_ok, cerca_notizie=notizie_ok,
                              fonte_risoluzione=fonte_ok, mercati=mercati_ok, serie_storiche=serie_ok)
    bot = BotPrevisioni(impostazioni=imp, registro=registro, modelli_previsione=modelli, modello_ricerca=ricerca,
                        modello_riassunto=riassunto, ricercatore=ricercatore, torneo="prova")
    rapporti = await bot.forecast_questions(domande_di_prova()[:1], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException)
    conteggi = dict(registro.interroga("SELECT modello, COUNT(*) FROM chiamate WHERE scopo='previsione' GROUP BY modello"))
    assert conteggi == {"finto/a": 2, "finto/b": 2, "finto/c": 2}
    assert sum("OUTSIDE VIEW" in p for p in LlmCheRegistraPrompt.prompt_visti) == 3


# ------------------------------------------------------------ bot secondario
def test_bot_secondario_usa_token_2_e_registri_separati(monkeypatch):
    import main as avvio

    for nome in ("METACULUS_TOKEN_2", "PERCORSO_REGISTRO", "PERCORSO_PAGELLA"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("METACULUS_TOKEN", "token-primario-finto")
    assert avvio.configura_bot_secondario() is False
    assert os.environ["METACULUS_TOKEN"] == "token-primario-finto"
    monkeypatch.setenv("METACULUS_TOKEN_2", "token-secondario-finto")
    assert avvio.configura_bot_secondario() is True
    assert os.environ["METACULUS_TOKEN"] == "token-secondario-finto"
    assert os.environ["PERCORSO_REGISTRO"].endswith("registro_v2.sqlite")
    assert os.environ["PERCORSO_PAGELLA"].endswith("pagella_v2.sqlite")
    assert Impostazioni.da_ambiente().percorso_registro.name == "registro_v2.sqlite"


def test_workflow_v2_spento_per_impostazione():
    from pathlib import Path

    testo = Path(__file__).resolve().parent.parent.joinpath(".github", "workflows", "previsioni-v2.yaml").read_text(encoding="utf-8")
    assert "schedule:" not in testo and "workflow_dispatch:" in testo
    assert "if: vars.BOT_2_ATTIVO == 'si'" in testo and "--bot 2" in testo
    assert "METACULUS_TOKEN_2: ${{ secrets.METACULUS_TOKEN_2 }}" in testo
