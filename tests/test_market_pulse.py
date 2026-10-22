"""
Market Pulse con dati finti, nessuna rete: domande di gruppo nelle due forme
plausibili del JSON, calendario (prima previsione e aggiornamento nelle ultime 6
ore), doppia previsione solo in questo torneo, tetti propri, prompt e ricerca,
main e workflow.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from forecasting_tools import MetaculusClient, NumericQuestion

from bot import market_pulse as mp
from bot.config import Impostazioni
from bot.domande_prova import domande_di_prova
from bot.llm import LlmFinto
from bot.previsore import BotPrevisioni, chiave_domanda
from bot.ricerca import Ricercatore
from bot.spese import Registro, TettiTorneo, TettoSuperato, inizio_passata, torneo_corrente

ADESSO = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(istante: datetime) -> str:
    return istante.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sotto(id_, etichetta, titolo, spot=None, chiusura=None, stato="open", **extra) -> dict:
    """Una sotto-domanda numerica come la manda l'API (ricavi in dollari)."""
    chiusura = chiusura or ADESSO + timedelta(days=10)
    sotto = {
        "id": id_, "title": titolo, "label": etichetta, "type": "numeric",
        "open_time": _iso(ADESSO - timedelta(days=5)), "scheduled_close_time": _iso(chiusura),
        "scheduled_resolve_time": _iso(chiusura + timedelta(days=7)),
        "scaling": {"range_min": 50e9, "range_max": 80e9, "zero_point": None},
        "open_lower_bound": True, "open_upper_bound": True, "unit": "USD",
        "description": "", "resolution_criteria": "", "fine_print": "",
    }
    if stato is not None:
        sotto["status"] = stato
    if spot is not None:
        sotto["spot_scoring_time"] = _iso(spot)
    sotto.update(extra)
    return sotto


def post_forma_a() -> dict:
    """Forma standard: testi a livello di gruppo, stato e spot_scoring_time su ogni sotto-domanda."""
    return {
        "id": 40001, "title": "Nvidia fiscal Q3 2027 results", "status": "open",
        "projects": {"tournament": [{"slug": "market-pulse-26q4"}], "default_project": {"id": 33200, "slug": "market-pulse-26q4"}},
        "group_of_questions": {
            "description": "Nvidia reports fiscal Q3 2027 results in November 2026.",
            "resolution_criteria": "Resolves to the figure in Nvidia's quarterly earnings press release.",
            "fine_print": "GAAP figures in USD as first reported.",
            "questions": [
                # titolo uguale a quello del gruppo: serve l'etichetta per capire la sotto-domanda
                _sotto(50001, "Revenue", "Nvidia fiscal Q3 2027 results", spot=ADESSO + timedelta(days=2), chiusura=ADESSO + timedelta(days=3)),
                # titolo specifico che contiene già l'etichetta
                _sotto(50002, "Data center revenue", "What will Nvidia's Data center revenue be in fiscal Q3 2027?",
                       spot=ADESSO + timedelta(days=2), chiusura=ADESSO + timedelta(days=3)),
                # non ancora aperta: resta fuori
                _sotto(50003, "Q4 revenue guidance", "Nvidia fiscal Q3 2027 results", stato="upcoming",
                       open_time=_iso(ADESSO + timedelta(days=1))),
            ],
        },
    }


def post_forma_b() -> dict:
    """Forma alternativa: niente testi né progetto predefinito a livello di gruppo, niente stato né spot."""
    return {
        "id": 40002, "title": "Apple September-quarter 2026", "status": "open",
        "projects": {"tournament": [{"slug": "market-pulse-26q4"}]},
        "group_of_questions": {
            "questions": [
                _sotto(50011, "Revenue", "Revenue", stato=None, chiusura=ADESSO + timedelta(days=4),
                       description="Apple's fiscal Q4 2026 ends in late September.",
                       resolution_criteria="Resolves to total net sales in Apple's 10-K for the quarter."),
            ],
        },
    }


def post_singolo() -> dict:
    """Un post non di gruppo con una sola domanda numerica."""
    domanda = _sotto(50021, None, "How many vehicles will Tesla deliver in Q4 2026?", spot=ADESSO + timedelta(days=60),
                     chiusura=ADESSO + timedelta(days=61), resolution_criteria="Tesla's Q4 2026 delivery report.")
    domanda.pop("label")
    domanda["scaling"] = {"range_min": 300000, "range_max": 700000, "zero_point": None}
    return {"id": 40003, "title": domanda["title"], "status": "open",
            "projects": {"default_project": {"id": 33200}}, "question": domanda}


class ClientFinto:
    """Come MetaculusClient.get_all_open_questions_from_tournament: spacchetta con la libreria, scarta i post illeggibili."""

    def __init__(self, posts, guasto=False):
        self.posts, self.guasto = posts, guasto
        self.chiamate = 0

    def get_all_open_questions_from_tournament(self, slug):
        self.chiamate += 1
        if self.guasto:
            raise ConnectionError("API giù")
        domande = []
        for post in self.posts:
            try:
                domande += MetaculusClient()._post_json_to_questions_while_handling_groups(post, "unpack_subquestions")
            except Exception:
                continue  # la libreria registra un avviso e va avanti
        return [d for d in domande if d.state is not None and d.state.value == "open"]


# ------------------------------------------------------------ forme del JSON
def test_la_libreria_spacchetta_i_gruppi_in_numeric_question():
    domande = MetaculusClient._unpack_group_question(post_forma_a())
    assert [type(d) for d in domande] == [NumericQuestion] * 3
    ricavi = domande[0]
    assert ricavi.group_question_option == "Revenue" and ricavi.question_ids_of_group == [50001, 50002, 50003]
    assert chiave_domanda(ricavi) == "40001-50001" and ricavi.page_url.endswith("/40001")
    assert ricavi.resolution_criteria.startswith("Resolves to the figure")
    # Spot scoring letto dal JSON della sotto-domanda; scadenza = il primo tra spot e chiusura.
    assert mp.momento_spot(ricavi) == ADESSO + timedelta(days=2)
    assert mp.scadenza(ricavi) == ADESSO + timedelta(days=2)
    assert mp.testo_completo(ricavi) == "Nvidia fiscal Q3 2027 results — Revenue"
    assert mp.testo_completo(domande[1]) == "What will Nvidia's Data center revenue be in fiscal Q3 2027?"


def test_lettura_diretta_forma_a_come_la_libreria():
    dalla_libreria = {chiave_domanda(d): d for d in MetaculusClient._unpack_group_question(post_forma_a())}
    dirette = {chiave_domanda(d): d for d in mp.estrai_domande(post_forma_a())}
    assert set(dirette) == set(dalla_libreria) == {"40001-50001", "40001-50002", "40001-50003"}
    for chiave, d in dirette.items():
        libreria = dalla_libreria[chiave]
        assert (d.lower_bound, d.upper_bound, d.open_upper_bound, d.close_time) == (
            libreria.lower_bound, libreria.upper_bound, libreria.open_upper_bound, libreria.close_time)
        assert d.state == libreria.state and mp.scadenza(d) == mp.scadenza(libreria)
        assert d.resolution_criteria == libreria.resolution_criteria


def test_forma_b_la_libreria_la_scarta_la_lettura_diretta_no():
    with pytest.raises(KeyError):
        MetaculusClient._unpack_group_question(post_forma_b())
    [domanda] = mp.estrai_domande(post_forma_b())
    assert isinstance(domanda, NumericQuestion) and domanda.state.value == "open"  # stato dedotto dalle date
    assert "total net sales" in domanda.resolution_criteria  # criteri della sotto-domanda
    assert mp.momento_spot(domanda) is None
    assert mp.scadenza(domanda) == ADESSO + timedelta(days=4)  # senza spot: la chiusura
    assert mp.testo_completo(domanda) == "Apple September-quarter 2026 — Revenue"
    [tesla] = mp.estrai_domande(post_singolo())
    assert chiave_domanda(tesla) == "40003-50021" and tesla.group_question_option is None
    assert mp.testo_completo(tesla) == "How many vehicles will Tesla deliver in Q4 2026?"
    assert mp.estrai_domande({"id": 1, "notebook": {}}) == []


def test_criteri_del_gruppo_e_della_sotto_domanda_si_sommano():
    post = post_forma_a()
    post["group_of_questions"]["questions"][0]["resolution_criteria"] = "Use the total revenue line."
    ricavi = mp.estrai_domande(post)[0]
    assert ricavi.resolution_criteria == (
        "Resolves to the figure in Nvidia's quarterly earnings press release.\n\nUse the total revenue line."
    )


def test_domande_aperte_lettura_diretta_poi_libreria_solo_se_serve():
    posts = [post_forma_a(), post_forma_b(), post_singolo()]
    attese = {"40001-50001", "40001-50002", "40002-50011", "40003-50021"}  # 50003 non è aperta
    client = ClientFinto(posts)
    domande = mp.domande_aperte(client, "market-pulse-26q4", scarica_post=lambda slug: posts)
    assert {chiave_domanda(d) for d in domande} == attese
    assert client.chiamate == 0  # tutto letto con una richiesta: la libreria (e le sue attese) non serve
    # La libreria da sola perde il gruppo in forma B.
    assert {chiave_domanda(d) for d in client.get_all_open_questions_from_tournament("x")} == attese - {"40002-50011"}

    # Nessun post aperto: lista vuota, senza chiamare la libreria (giro di pochi secondi).
    client = ClientFinto(posts)
    assert mp.domande_aperte(client, "market-pulse-26q4", scarica_post=lambda slug: []) == [] and client.chiamate == 0

    def lettura_guasta(slug):
        raise ConnectionError("403")

    client = ClientFinto(posts)
    solo_libreria = mp.domande_aperte(client, "market-pulse-26q4", scarica_post=lettura_guasta)
    assert {chiave_domanda(d) for d in solo_libreria} == attese - {"40002-50011"} and client.chiamate == 1

    # Una sotto-domanda che la lettura diretta non capisce: si chiama anche la libreria e si unisce.
    strano = post_forma_a()
    del strano["group_of_questions"]["questions"][1]["scaling"]
    da_libreria = ClientFinto([post_forma_a()])
    unite = mp.domande_aperte(da_libreria, "market-pulse-26q4", scarica_post=lambda slug: [strano])
    assert {chiave_domanda(d) for d in unite} == {"40001-50001", "40001-50002"} and da_libreria.chiamate == 1

    # La libreria guasta quando la lettura diretta ha funzionato: si va avanti con quello che c'è.
    unite = mp.domande_aperte(ClientFinto([], guasto=True), "market-pulse-26q4", scarica_post=lambda slug: [strano])
    assert {chiave_domanda(d) for d in unite} == {"40001-50001"}
    with pytest.raises(RuntimeError, match="domande non lette"):
        mp.domande_aperte(ClientFinto(posts, guasto=True), "market-pulse-26q4", scarica_post=lettura_guasta)


def test_arricchisci_aggiunge_il_contesto_del_gruppo_una_volta():
    ricavi = mp.estrai_domande(post_forma_a())[0]
    mp.arricchisci(mp.arricchisci(ricavi))
    assert ricavi.question_text == "Nvidia fiscal Q3 2027 results — Revenue"
    assert ricavi.background_info.count("Market Pulse question group") == 1
    assert "Data center revenue; Q4 revenue guidance" in ricavi.background_info
    assert "Nvidia reports fiscal Q3 2027 results" in ricavi.background_info


def test_momenti_api_dalla_storia_delle_nostre_previsioni():
    post = post_forma_a()
    prima = ADESSO - timedelta(days=2)
    post["group_of_questions"]["questions"][0]["my_forecasts"] = {
        "history": [{"start_time": prima.timestamp(), "end_time": None, "forecast_values": [0.0] * 201}]
    }
    ricavi = mp.estrai_domande(post)[0]
    assert mp.momenti_api(ricavi) == [prima]


# ------------------------------------------------------------ calendario
def test_decidi_fase_finestra_delle_sei_ore():
    adesso = ADESSO
    tra = lambda ore: adesso + timedelta(hours=ore)  # noqa: E731
    fa = lambda giorni: adesso - timedelta(days=giorni)  # noqa: E731
    assert mp.decidi_fase(tra(72), [], adesso)[0] == mp.PRIMA
    assert mp.decidi_fase(None, [], adesso)[0] == mp.PRIMA
    assert mp.decidi_fase(tra(3), [fa(2)], adesso)[0] == mp.AGGIORNAMENTO
    assert mp.decidi_fase(tra(6), [fa(2)], adesso)[0] == mp.AGGIORNAMENTO  # esattamente 6 ore: dentro
    assert mp.decidi_fase(tra(6.5), [fa(2)], adesso) == (None, "aggiornamento più avanti")
    assert mp.decidi_fase(tra(3), [adesso - timedelta(hours=20)], adesso) == (None, "prima previsione di meno di un giorno fa")
    assert mp.decidi_fase(tra(3), [fa(2), fa(1)], adesso) == (None, "già aggiornata")
    assert mp.decidi_fase(tra(-1), [], adesso) == (None, "spot scoring passato")
    assert mp.decidi_fase(None, [fa(2)], adesso)[0] is None
    assert mp.decidi_fase(tra(0.1), [], adesso, minuti_minimi=12) == (None, "troppo vicina allo spot scoring")


def test_pianifica_prima_gli_aggiornamenti_poi_lo_spot_piu_vicino(tmp_path):
    registro = Registro(tmp_path / "r.sqlite", 1, 10, 100)
    vicina = _domanda(50101, spot=ADESSO + timedelta(days=1))
    lontana = _domanda(50102, spot=ADESSO + timedelta(days=9))
    da_aggiornare = _domanda(50103, spot=ADESSO + timedelta(hours=4))
    registro.registra_previsione(chiave_domanda(da_aggiornare), "u", "t", mp.NOME_TORNEO, "numeric", {"p50": 1}, inviata=False)
    _sposta_indietro(registro, giorni=2)
    scelte = mp.pianifica([lontana, vicina, da_aggiornare], registro, inviata=False, adesso=ADESSO)
    assert [(chiave_domanda(d), f) for d, f in scelte] == [
        ("40001-50103", mp.AGGIORNAMENTO), ("40001-50101", mp.PRIMA), ("40001-50102", mp.PRIMA)]
    assert len(mp.pianifica([lontana, vicina, da_aggiornare], registro, inviata=False, adesso=ADESSO, massimo=2)) == 2
    # Una previsione di un ALTRO torneo con la stessa chiave non conta per Market Pulse.
    registro.registra_previsione(chiave_domanda(lontana), "u", "t", "stagione", "numeric", {"p50": 1}, inviata=False)
    assert ("40001-50102", mp.PRIMA) in [(chiave_domanda(d), f) for d, f in mp.pianifica([lontana], registro, False, ADESSO)]
    registro.chiudi()


def test_pianifica_usa_la_storia_dell_api_quando_si_invia(tmp_path):
    registro = Registro(tmp_path / "r.sqlite", 1, 10, 100)
    post = post_forma_a()
    post["group_of_questions"]["questions"][0]["my_forecasts"] = {"history": [{"start_time": (ADESSO - timedelta(hours=3)).timestamp()}]}
    ricavi = mp.estrai_domande(post)[0]
    # Registro vuoto (per esempio non salvato) ma l'API dice che abbiamo già previsto: niente seconda "prima".
    assert mp.pianifica([ricavi], registro, inviata=True, adesso=ADESSO) == []
    # In prova (niente invio) la storia dell'API non conta.
    assert [f for _, f in mp.pianifica([ricavi], registro, inviata=False, adesso=ADESSO)] == [mp.PRIMA]
    registro.chiudi()


def _domanda(id_, spot, chiusura=None):
    post = post_forma_a()
    sotto = _sotto(id_, "Revenue", "Nvidia fiscal Q3 2027 results", spot=spot, chiusura=chiusura or spot + timedelta(hours=1))
    post["group_of_questions"]["questions"] = [sotto]
    return mp.estrai_domande(post)[0]


def _sposta_indietro(registro: Registro, giorni: float) -> None:
    """Simula il passare del tempo: le previsioni e le chiamate già registrate diventano più vecchie."""
    for tabella in ("previsioni", "chiamate"):
        for id_, momento in registro.interroga(f"SELECT id, momento FROM {tabella}"):
            nuovo = (datetime.fromisoformat(momento) - timedelta(days=giorni)).isoformat(timespec="seconds")
            registro.interroga(f"UPDATE {tabella} SET momento = ? WHERE id = ?", (nuovo, id_))
    registro._db.commit()


# ------------------------------------------------------------ bot completo
async def web_ok(richiesta):
    web_ok.richieste.append(richiesta)
    return "- Consensus revenue 54.6 B USD (LSEG, 2026-10-20); guidance 54 B +/- 2% (2026-08-27)."


web_ok.richieste = []


async def notizie_ok(domanda):
    return "**Notizia finta**\nPublish date: October 20, 2026"


async def trimestrali_ok(domanda):
    return "Structured quarterly data for ticker NVDA: revenue 46.74 B USD (y/y +56%)."


class LlmCheRegistra(LlmFinto):
    prompt: list[str] = []

    async def _chiama(self, prompt):
        LlmCheRegistra.prompt.append(prompt if isinstance(prompt, str) else str(prompt))
        return await super()._chiama(prompt)


def costruisci_mp(tmp_path, tetti_mp=None, tetto_domanda=5.0, tetto_mese=100.0, classe=mp.BotMarketPulse, torneo="stagione"):
    imp = Impostazioni(modello_finto=True, modelli_previsione=["finto/claude-opus-5-5", "finto/claude-sonnet-5-5"],
                       modello_ricerca="finto/claude-sonnet-5-5", modello_riassunto="finto/claude-haiku-5-5",
                       tetto_domanda=tetto_domanda, tetto_mese=tetto_mese, percorso_registro=tmp_path / "r.sqlite")
    tetti = {mp.NOME_TORNEO: tetti_mp or TettiTorneo(mese=imp.tetto_mese_mp, giorno=imp.tetto_giorno_mp, domanda_totale=2 * tetto_domanda)}
    registro = Registro(imp.percorso_registro, imp.tetto_domanda, imp.tetto_giorno, imp.tetto_mese, tetti_torneo=tetti)
    modelli = [LlmCheRegistra(model=m, registro=registro, scopo="previsione", seme=i) for i, m in enumerate(imp.modelli_previsione)]
    ricerca = LlmFinto(model=imp.modello_ricerca, registro=registro, scopo="ricerca")
    riassunto = LlmFinto(model=imp.modello_riassunto, registro=registro, scopo="riassunto")
    fonti = dict(cerca_web=web_ok, cerca_notizie=notizie_ok)
    if classe is mp.BotMarketPulse:
        ricercatore = Ricercatore(imp, registro, ricerca, riassunto, trimestrali=trimestrali_ok, guida_web=mp.guida_web, **fonti)
        bot = mp.BotMarketPulse(impostazioni=imp, registro=registro, modelli_previsione=modelli, modello_ricerca=ricerca,
                                modello_riassunto=riassunto, ricercatore=ricercatore, massimo_per_giro=12)
    else:
        ricercatore = Ricercatore(imp, registro, ricerca, riassunto, **fonti)
        bot = BotPrevisioni(impostazioni=imp, registro=registro, modelli_previsione=modelli, modello_ricerca=ricerca,
                            modello_riassunto=riassunto, ricercatore=ricercatore, torneo=torneo)
    return bot, registro


def _riuscite(registro, torneo=mp.NOME_TORNEO):
    return registro.interroga("SELECT domanda, costo FROM previsioni WHERE errore IS NULL AND torneo = ? ORDER BY id", (torneo,))


async def test_market_pulse_prima_previsione_e_aggiornamento_nelle_ultime_sei_ore(tmp_path):
    LlmCheRegistra.prompt = []
    web_ok.richieste = []
    bot, registro = costruisci_mp(tmp_path)
    domanda = _domanda(50201, spot=ADESSO + timedelta(hours=3), chiusura=ADESSO + timedelta(hours=4))

    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)  # giro 1: prima previsione
    assert len(rapporti) == 1 and not isinstance(rapporti[0], BaseException), rapporti
    assert len(_riuscite(registro)) == 1
    assert await bot.forecast_questions([domanda], return_exceptions=True) == []  # meno di un giorno fa: niente

    costo_prima = _riuscite(registro)[0][1]
    _sposta_indietro(registro, giorni=2)  # la prima previsione ora ha due giorni
    LlmCheRegistra.prompt = []
    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)  # giro 3: aggiornamento
    assert len(rapporti) == 1 and not isinstance(rapporti[0], BaseException), rapporti
    assert sum("Answer format: percentiles" in p for p in LlmCheRegistra.prompt) == 5
    riuscite = _riuscite(registro)
    assert [r[0] for r in riuscite] == ["40001-50201", "40001-50201"]
    # Il costo registrato è quello della passata, non la somma di tutte (le due passate costano uguale).
    assert riuscite[1][1] == pytest.approx(costo_prima, rel=0.25)
    assert all("planned UPDATE of an earlier forecast" in p for p in LlmCheRegistra.prompt if "Answer format: percentiles" in p)

    _sposta_indietro(registro, giorni=2)
    assert await bot.forecast_questions([domanda], return_exceptions=True) == []  # già aggiornata: basta
    # Tutte le chiamate sono etichettate con il torneo.
    assert registro.interroga("SELECT DISTINCT torneo FROM chiamate") == [(mp.NOME_TORNEO,)]
    registro.chiudi()


async def test_doppia_previsione_solo_in_market_pulse(tmp_path):
    bot, registro = costruisci_mp(tmp_path, classe=BotPrevisioni, torneo="stagione")
    domanda = _domanda(50301, spot=ADESSO + timedelta(hours=3), chiusura=ADESSO + timedelta(hours=4))
    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException)
    _sposta_indietro(registro, giorni=2)
    # Stessa situazione che in Market Pulse farebbe l'aggiornamento: negli altri tornei la domanda non si rifà.
    assert await bot.forecast_questions([domanda], return_exceptions=True) == []
    assert len(_riuscite(registro, "stagione")) == 1
    assert bot.skip_previously_forecasted_questions is True and bot.passate_multiple is False
    assert registro.interroga("SELECT DISTINCT torneo FROM chiamate") == [("stagione",)]
    registro.chiudi()


async def test_prompt_e_ricerca_di_market_pulse(tmp_path):
    LlmCheRegistra.prompt = []
    web_ok.richieste = []
    bot, registro = costruisci_mp(tmp_path)
    domanda = _domanda(50401, spot=ADESSO + timedelta(days=2))
    rapporti = await bot.forecast_questions([domanda], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException), rapporti
    numerici = [p for p in LlmCheRegistra.prompt if "Answer format: percentiles" in p]
    assert numerici
    for p in numerici:
        assert "ADMINISTERED" in p and "history of surprises" in p and "OPEN bound" in p
        assert "Spot scoring: only the forecast standing at" in p and "planned UPDATE" not in p
        assert "Nvidia fiscal Q3 2027 results — Revenue" in p and "Market Pulse question group" in p
    assert "Priorities for this search" in web_ok.richieste[0] and "last 8 quarters" in web_ok.richieste[0]
    ricerca = await bot.ricercatore.ricerca(domanda)
    assert "Source: quarterly results" in ricerca and "Source: data series" not in ricerca

    # Il bot degli altri tornei non riceve né la nota né la guida.
    LlmCheRegistra.prompt = []
    web_ok.richieste = []
    altro, registro2 = costruisci_mp(tmp_path / "altro", classe=BotPrevisioni)
    await altro.forecast_questions([domande_di_prova()[1]], return_exceptions=True)
    assert not any("ADMINISTERED" in p for p in LlmCheRegistra.prompt)
    assert not any("Priorities for this search" in r for r in web_ok.richieste)
    registro.chiudi()
    registro2.chiudi()


# ------------------------------------------------------------ tetti
async def test_tetto_mensile_proprio_contato_filtrando_per_torneo(tmp_path):
    bot, registro = costruisci_mp(tmp_path, tetti_mp=TettiTorneo(mese=1.0, giorno=50.0, domanda_totale=10.0))
    # 5 $ spesi questo mese in un altro torneo non toccano il tetto di Market Pulse.
    registro.registra_chiamata("x", 0, 0, costo=5.0, domanda="altro", torneo="stagione")
    rapporti = await bot.forecast_questions([_domanda(50501, spot=ADESSO + timedelta(days=2))], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException), rapporti
    assert registro.spesa_mese_torneo(mp.NOME_TORNEO) == pytest.approx(registro.spesa_mese() - 5.0)

    # Market Pulse arriva a 1 $: le sue domande si fermano senza spendere (prime previsioni
    # rimandate dal calendario, aggiornamenti fermati dal tetto)...
    da_aggiornare = _domanda(50502, spot=ADESSO + timedelta(hours=3))
    registro.registra_previsione(chiave_domanda(da_aggiornare), "u", "t", mp.NOME_TORNEO, "numeric", {"p50": 1}, inviata=False)
    _sposta_indietro(registro, giorni=2)
    registro.registra_chiamata("x", 0, 0, costo=1.0, domanda="vecchia", torneo=mp.NOME_TORNEO)
    chiamate = registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0]
    assert await bot.forecast_questions([_domanda(50503, spot=ADESSO + timedelta(days=2))], return_exceptions=True) == []
    rapporti = await bot.forecast_questions([da_aggiornare], return_exceptions=True)
    assert isinstance(rapporti[0], BaseException) and "tetto mensile market_pulse" in str(rapporti[0])
    assert registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0] == chiamate

    # ...ma gli altri tornei, sullo stesso registro, vanno avanti.
    altro = BotPrevisioni(impostazioni=bot.imp, registro=registro, modelli_previsione=bot.modelli_previsione,
                          modello_ricerca=bot.ricercatore.modello_ricerca, modello_riassunto=bot.ricercatore.modello_riassunto,
                          ricercatore=bot.ricercatore, torneo="stagione")
    rapporti = await altro.forecast_questions([domande_di_prova()[0]], return_exceptions=True)
    assert not isinstance(rapporti[0], BaseException), rapporti
    registro.chiudi()


async def test_vicino_al_tetto_proprio_solo_aggiornamenti(tmp_path):
    bot, registro = costruisci_mp(tmp_path, tetto_domanda=0.9, tetti_mp=TettiTorneo(mese=30.0, giorno=3.0, domanda_totale=1.8))
    da_aggiornare = _domanda(50701, spot=ADESSO + timedelta(hours=3))
    nuova = _domanda(50702, spot=ADESSO + timedelta(days=2))
    registro.registra_previsione(chiave_domanda(da_aggiornare), "u", "t", mp.NOME_TORNEO, "numeric", {"p50": 1}, inviata=False)
    _sposta_indietro(registro, giorni=2)
    # Oggi Market Pulse ha speso 2,40 $ su 3: oltre 3 - min(2 x 0,90; 3 / 4) = 2,25, quindi solo l'aggiornamento.
    registro.registra_chiamata("x", 0, 0, costo=2.4, domanda="altra", torneo=mp.NOME_TORNEO)
    rapporti = await bot.forecast_questions([nuova, da_aggiornare], return_exceptions=True)
    assert len(rapporti) == 1 and rapporti[0].question.id_of_question == 50701
    # Lontano dai tetti si fanno entrambe.
    bot2, registro2 = costruisci_mp(tmp_path / "largo", tetto_domanda=0.9)
    assert len(await bot2.forecast_questions([_domanda(50703, spot=ADESSO + timedelta(days=2))], return_exceptions=True)) == 1
    registro.chiudi()
    registro2.chiudi()


async def test_il_tetto_mensile_generale_vale_anche_per_market_pulse(tmp_path):
    bot, registro = costruisci_mp(tmp_path, tetto_mese=1.0)
    registro.registra_chiamata("x", 0, 0, costo=1.0, domanda="altro", torneo="stagione")
    rapporti = await bot.forecast_questions([_domanda(50601, spot=ADESSO + timedelta(days=2))], return_exceptions=True)
    assert isinstance(rapporti[0], BaseException) and "tetto mensile raggiunto" in str(rapporti[0])
    registro.chiudi()


def test_tetto_per_passata_e_sul_totale(tmp_path):
    registro = Registro(tmp_path / "r.sqlite", tetto_domanda=0.5, tetto_giorno=100, tetto_mese=100,
                        tetti_torneo={mp.NOME_TORNEO: TettiTorneo(domanda_totale=1.0)})
    vecchio = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    registro.registra_chiamata("x", 0, 0, costo=0.6, domanda="d1", torneo=mp.NOME_TORNEO)
    registro.interroga("UPDATE chiamate SET momento = ?", (vecchio,))
    segni = [torneo_corrente.set(mp.NOME_TORNEO), inizio_passata.set(datetime.now(timezone.utc).isoformat(timespec="seconds"))]
    try:
        registro.controlla_tetti("d1")  # la prima passata (0,60 $) non conta per la seconda
        registro.registra_chiamata("x", 0, 0, costo=0.45, domanda="d1")
        with pytest.raises(TettoSuperato, match="tutte le passate"):
            registro.controlla_tetti("d1")  # 1,05 $ in tutto: oltre il doppio del tetto per domanda
    finally:
        inizio_passata.reset(segni[1])
        torneo_corrente.reset(segni[0])
    with pytest.raises(TettoSuperato, match="tetto per domanda raggiunto"):
        registro.controlla_tetti("d1")  # senza passate (altri tornei) conta tutta la storia, come prima
    registro.chiudi()


def test_registro_vecchio_senza_colonna_torneo(tmp_path):
    percorso = tmp_path / "vecchio.sqlite"
    db = sqlite3.connect(percorso)
    db.execute(
        "CREATE TABLE chiamate (id INTEGER PRIMARY KEY, momento TEXT NOT NULL, domanda TEXT, scopo TEXT, modello TEXT NOT NULL,"
        " token_ingresso INTEGER NOT NULL, token_uscita INTEGER NOT NULL, ricerche_web INTEGER NOT NULL DEFAULT 0,"
        " costo REAL NOT NULL, esito TEXT NOT NULL DEFAULT 'ok')"
    )
    db.execute("INSERT INTO chiamate (momento, domanda, modello, token_ingresso, token_uscita, costo) VALUES (?, 'd', 'm', 0, 0, 2.5)",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    db.commit()
    db.close()
    registro = Registro(percorso, 1, 10, 100)
    assert "torneo" in {r[1] for r in registro.interroga("PRAGMA table_info(chiamate)")}
    assert registro.spesa_mese() == pytest.approx(2.5)
    assert registro.spesa_mese_torneo(mp.NOME_TORNEO) == 0.0
    registro.registra_chiamata("m", 0, 0, costo=0.5, torneo=mp.NOME_TORNEO)
    assert registro.spesa_mese_torneo(mp.NOME_TORNEO) == pytest.approx(0.5)
    registro.chiudi()


# ------------------------------------------------------------ main, impostazioni, workflow, rapporto
def test_impostazioni_market_pulse(monkeypatch):
    for nome in ("TORNEO_MARKET_PULSE", "TETTO_MESE_MP_USD", "TETTO_GIORNO_MP_USD", "MP_DOMANDE_PER_GIRO"):
        monkeypatch.delenv(nome, raising=False)
    imp = Impostazioni.da_ambiente()
    assert imp.torneo_market_pulse == "market-pulse-26q4" == MetaculusClient.CURRENT_MARKET_PULSE_ID
    assert (imp.tetto_mese_mp, imp.tetto_giorno_mp, imp.mp_domande_per_giro) == (30.0, 8.0, 12)
    monkeypatch.setenv("TORNEO_MARKET_PULSE", " market-pulse-27q1 ")
    monkeypatch.setenv("TETTO_MESE_MP_USD", "12,5")
    imp = Impostazioni.da_ambiente()
    assert imp.torneo_market_pulse == "market-pulse-27q1" and imp.tetto_mese_mp == 12.5
    monkeypatch.setenv("TORNEO_MARKET_PULSE", "")  # variabile del repo vuota: il predefinito
    assert Impostazioni.da_ambiente().torneo_market_pulse == "market-pulse-26q4"


def test_main_costruisce_il_bot_market_pulse_e_il_registro_con_i_tetti(tmp_path):
    import main as avvio

    imp = Impostazioni(modello_finto=True, modelli_previsione=["finto/a"], modello_ricerca="finto/r",
                       modello_riassunto="finto/s", percorso_registro=tmp_path / "r.sqlite", tetto_domanda=0.9)
    registro = avvio.crea_registro(imp)
    assert registro.tetti_torneo[mp.NOME_TORNEO] == TettiTorneo(mese=30.0, giorno=8.0, domanda_totale=1.8)
    bot = avvio.costruisci_bot(imp, registro, mp.NOME_TORNEO, False, False, rifai=True, market_pulse=True)
    assert isinstance(bot, mp.BotMarketPulse) and bot.torneo == mp.NOME_TORNEO
    assert bot.skip_previously_forecasted_questions is False and bot.massimo_per_giro == 12
    assert bot.ricercatore._guida_web is mp.guida_web
    normale = avvio.costruisci_bot(imp, registro, "stagione", False, False, rifai=False)
    assert type(normale) is BotPrevisioni and normale.ricercatore._guida_web is None
    registro.chiudi()


def test_main_modo_market_pulse_da_capo_a_fondo(monkeypatch, tmp_path, capsys):
    """main --modo market_pulse con modelli finti e API finta: prevede le sotto-domande aperte, poi non le rifà."""
    import main as avvio

    posts = [post_forma_a(), post_forma_b()]
    letture = []

    def scarica_post(slug):
        letture.append(slug)
        return posts

    for nome in ("INVIA_PREVISIONI", "MODELLI_PREVISIONE", "TORNEO_MARKET_PULSE", "PREVISIONI_PER_DOMANDA"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("MODELLO_FINTO", "si")
    monkeypatch.setenv("METACULUS_TOKEN", "token-finto")
    monkeypatch.setenv("PERCORSO_REGISTRO", str(tmp_path / "registro.sqlite"))
    monkeypatch.setattr(mp, "scarica_post_aperti", scarica_post)
    monkeypatch.setattr(avvio, "MetaculusClient", lambda: ClientFinto([]))
    monkeypatch.setattr("sys.argv", ["main.py", "--modo", "market_pulse"])

    assert avvio.main() == 0
    assert letture == ["market-pulse-26q4"]
    registro = Registro(tmp_path / "registro.sqlite", 1, 1, 1)
    righe = registro.interroga("SELECT domanda, torneo, inviata, errore FROM previsioni ORDER BY domanda")
    assert righe == [
        ("40001-50001", mp.NOME_TORNEO, 0, None), ("40001-50002", mp.NOME_TORNEO, 0, None), ("40002-50011", mp.NOME_TORNEO, 0, None),
    ]
    assert registro.spesa_mese() > 0 and registro.spesa_mese_torneo(mp.NOME_TORNEO) == pytest.approx(registro.spesa_mese())
    registro.chiudi()
    assert "Market Pulse (market-pulse-26q4): tetti propri 30.00 $/mese" in capsys.readouterr().out

    assert avvio.main() == 0  # secondo giro subito dopo: niente da fare, nessuna spesa
    registro = Registro(tmp_path / "registro.sqlite", 1, 1, 1)
    assert registro.interroga("SELECT COUNT(*) FROM previsioni")[0][0] == 3
    registro.chiudi()


def test_main_rifiuta_market_pulse_con_il_bot_secondario(monkeypatch, capsys):
    import main as avvio

    monkeypatch.setattr("sys.argv", ["main.py", "--modo", "market_pulse", "--bot", "2"])
    monkeypatch.setenv("METACULUS_TOKEN_2", "token-secondario-finto")
    assert avvio.main() == 1
    assert "una volta sola" in capsys.readouterr().out


def test_workflow_passo_market_pulse():
    import yaml

    percorso = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "previsioni.yaml"
    flusso = yaml.safe_load(percorso.read_text(encoding="utf-8"))
    assert list(flusso["jobs"]) == ["previsioni"]  # stesso job: nessun avvio di runner in più
    passi = flusso["jobs"]["previsioni"]["steps"]
    nomi = [p.get("name", "") for p in passi]
    indice = next(i for i, n in enumerate(nomi) if n.startswith("Market Pulse"))
    assert nomi[indice - 1] == "Fai le previsioni"  # dopo il torneo principale
    assert indice < nomi.index("Salva il registro delle spese e delle previsioni")
    passo = passi[indice]
    assert passo["if"] == "vars.MARKET_PULSE_ATTIVO == 'si'" and passo["continue-on-error"] is True
    assert passo["run"] == "uv run --no-sync python main.py --modo market_pulse"
    ambiente = passo["env"]
    assert ambiente["METACULUS_TOKEN"] == "${{ secrets.METACULUS_TOKEN }}"  # il bot principale, mai un altro account
    assert "METACULUS_TOKEN_2" not in ambiente
    assert ambiente["TETTO_MESE_MP_USD"] == "${{ vars.TETTO_MESE_MP_USD }}"
    assert ambiente["INVIA_PREVISIONI"] == "${{ vars.INVIA_PREVISIONI }}"
    # Il registro di Market Pulse è quello principale: il passo di salvataggio lo prende già.
    assert "dati/registro.sqlite" in passi[-1]["run"]


def test_rapporto_con_market_pulse(tmp_path, monkeypatch):
    from bot.rapporto import scrivi_rapporto

    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    imp = Impostazioni(percorso_registro=tmp_path / "r.sqlite")
    registro = Registro(imp.percorso_registro, 1, 1, 1)
    registro.registra_chiamata("anthropic/claude-opus-5-5", 1000, 1000, domanda="40001-50001", torneo=mp.NOME_TORNEO)
    registro.registra_previsione("40001-50001", "u", "t", mp.NOME_TORNEO, "numeric", {"p50": 1}, inviata=False)
    registro.registra_previsione("40001-50001", "u", "t", mp.NOME_TORNEO, "numeric", {"p50": 2}, inviata=False)
    registro.chiudi()
    testo = scrivi_rapporto(imp, cartella=tmp_path / "rapporti").read_text(encoding="utf-8")
    assert "market_pulse: 2 previste" in testo
    assert "Market Pulse, mese" in testo and "tetto proprio di 30 $" in testo
