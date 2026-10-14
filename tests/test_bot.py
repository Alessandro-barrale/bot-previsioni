"""Prove del bot completo con il modello finto: nessuna rete, nessun invio."""
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
    assert 0.01 <= binaria.prediction <= 0.99
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
