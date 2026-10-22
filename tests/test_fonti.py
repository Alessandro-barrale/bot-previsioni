"""Fonti v2 (fonte di risoluzione, mercati, serie storiche) con risposte finte: nessuna rete."""
from datetime import date, datetime, timedelta, timezone

import pytest

from bot.domande_prova import domande_di_prova
from bot.fonti import fonte_risoluzione as fr
from bot.fonti import fred
from bot.fonti import mercati as mk
from bot.fonti import serie_storiche as ss
from bot.llm import LlmFinto
from bot.spese import Registro


@pytest.fixture
def modello(tmp_path):
    registro = Registro(tmp_path / "r.sqlite", 5, 50, 500)
    yield LlmFinto(model="finto/claude-haiku-5-5", registro=registro, scopo="riassunto", seme=1)
    registro.chiudi()


# ------------------------------------------------------ fonte di risoluzione
def test_estrai_url_pulisce_e_salta():
    testo = (
        "See https://www.ecb.europa.eu/press/pr/date/2026/html/index.en.html). Data at "
        "https://fred.stlouisfed.org/series/CPIAUCSL, not https://www.metaculus.com/questions/1/ nor "
        "https://example.org/report.pdf. Again: https://fred.stlouisfed.org/series/CPIAUCSL"
    )
    assert fr.estrai_url(testo, None) == [
        "https://www.ecb.europa.eu/press/pr/date/2026/html/index.en.html",
        "https://fred.stlouisfed.org/series/CPIAUCSL",
    ]


def test_estrai_url_markdown_metaculus():
    # I testi Metaculus sono markdown: escape con backslash, link [testo](url), URL tra parentesi.
    testo = (
        r"Per https://en.wikipedia.org/wiki/Electoral_College_\(United_States\). "
        r"See [the index](https://a.example/some\_page\*). Also (https://b.example/x). "
        "Then https://c.example/y), and https://d.example/z(1)."
    )
    assert fr.estrai_url(testo) == [
        "https://en.wikipedia.org/wiki/Electoral_College_(United_States)",
        "https://a.example/some_page*",
        "https://b.example/x",
        "https://c.example/y",
        "https://d.example/z(1)",
    ]
    assert fr.rifinisci_url("https://x.example/a_(b)).") == "https://x.example/a_(b)"
    assert fr.rifinisci_url("https://x.example/a?b=1&amp;c=2;") == "https://x.example/a?b=1&c=2"


def test_pulisci_html():
    pagina = "<html><head><title>x</title><style>a{}</style></head><body><nav>menu</nav><h1>Rate&nbsp;decision</h1><script>var a=1;</script><p>Held at <b>2.00%</b>.</p></body></html>"
    testo = fr.pulisci_html(pagina)
    assert "Rate decision" in testo and "Held at 2.00%" in testo
    assert "menu" not in testo and "var a" not in testo and "<" not in testo


async def test_fonte_risoluzione_limiti_e_guasti():
    domanda = domande_di_prova()[0]
    domanda.resolution_criteria = "Per https://a.example/one and https://b.example/two and https://c.example/three"

    def scarica(url):
        if "b.example" in url:
            raise ConnectionError("giù")
        return "parola " * 5000, "2026-10-20"

    testo = await fr.fonte_risoluzione(domanda, scarica_pagina=scarica, max_caratteri=6000)
    assert testo.startswith("What the resolution source says NOW")
    assert "https://a.example/one" in testo and "Page date: 2026-10-20" in testo
    assert "https://b.example/two" in testo and "could not be fetched today: ConnectionError" in testo
    assert "c.example" not in testo  # al massimo 2 URL
    assert len(testo) < 6000 + 600


CSV_FRED = (
    "observation_date,UNRATE\n"
    + "\n".join(f"2025-{m:02d}-01,{4 + m / 10}" for m in range(1, 13))
    + "\n2026-01-01,.\n2026-02-01,4.4\n"
)


def test_fred_csv_lettura_e_descrizione():
    assert fred.id_da_url("https://fred.stlouisfed.org/series/CPIAUCSL") == "CPIAUCSL"
    assert fred.id_da_url("https://fred.stlouisfed.org/series/dgs10#0") == "DGS10"
    assert fred.id_da_url("https://www.ecb.europa.eu/stats") is None
    serie = fred.leggi_csv(CSV_FRED)
    assert len(serie) == 13 and serie[-1] == (date(2026, 2, 1), 4.4)  # "." saltato, intestazione saltata
    testo = fred.descrivi_ultime("UNRATE", serie)
    assert testo.startswith("FRED series UNRATE (public CSV, no key). LATEST VALUE: 4.4 on 2026-02-01.")
    assert "Last 12 observations" in testo and "- 2026-02-01: 4.4" in testo and "- 2025-01-01" not in testo
    assert "- 2025-02-01: 4.2" in testo
    assert fred.descrivi_ultime("X", []) == "FRED series X: no data."


def test_fred_scarica_csv_senza_chiave(monkeypatch):
    class Risposta:
        text = CSV_FRED

        def raise_for_status(self):
            pass

    viste = {}

    def finto_get(url, params=None, headers=None, timeout=None):
        viste["url"], viste["params"] = url, params
        return Risposta()

    monkeypatch.setattr(fred.requests, "get", finto_get)
    serie = fred.scarica_csv("UNRATE")
    assert viste["url"] == "https://fred.stlouisfed.org/graph/fredgraph.csv" and viste["params"] == {"id": "UNRATE"}
    assert len(serie) == 13

    Risposta.text = "observation_date,NOPE\n"
    with pytest.raises(ValueError):
        fred.scarica_csv("NOPE")


def test_scarica_devia_le_pagine_fred_sul_csv(monkeypatch):
    chiamate = []

    def finto_csv(serie, timeout=None):
        chiamate.append(serie)
        return fred.leggi_csv(CSV_FRED)

    monkeypatch.setattr(fred, "scarica_csv", finto_csv)
    monkeypatch.setattr(fr.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("niente HTML per FRED")))
    testo, data = fr.scarica("https://fred.stlouisfed.org/series/UNRATE")
    assert chiamate == ["UNRATE"] and data == "2026-02-01" and "LATEST VALUE: 4.4" in testo


async def test_fonte_risoluzione_assente_o_tutta_guasta():
    domanda = domande_di_prova()[0]
    assert await fr.fonte_risoluzione(domanda) is None  # nessun URL nei criteri
    domanda.fine_print = "https://a.example/one"

    def guasta(url):
        raise TimeoutError

    assert await fr.fonte_risoluzione(domanda, scarica_pagina=guasta) is None


# ------------------------------------------------------------------ mercati
def test_peso_liquidita():
    assert mk.peso_liquidita(None) == 0 and mk.peso_liquidita(500) == 0 and mk.peso_liquidita(1_000) == 0
    assert mk.peso_liquidita(10_000) == pytest.approx(0.04)
    assert mk.peso_liquidita(100_000) == 1.0 and mk.peso_liquidita(5_000_000) == 1.0
    assert 0 < mk.peso_liquidita(3_000) < 0.04 < mk.peso_liquidita(30_000) < 1.0
    assert mk.forza(0.0).startswith("negligible") and mk.forza(0.3) == "weak" and mk.forza(0.6) == "strong"


def test_proiezione_hazard():
    assert mk.proietta_hazard(0.3, 30, 30) == pytest.approx(0.3)
    assert mk.proietta_hazard(0.3, 30, 60) == pytest.approx(1 - 0.7**2)
    assert mk.proietta_hazard(0.3, 60, 30) == pytest.approx(1 - 0.7**0.5)
    assert mk.proietta_hazard(0.0, 30, 60) == 0.0 and mk.proietta_hazard(1.0, 30, 60) == 1.0
    assert mk.proietta_hazard(0.3, 0, 60) == 0.3


def test_parole_chiave():
    # Nomi propri prima; con almeno 2 nomi propri bastano loro (Manifold vuole tutte le parole del termine).
    assert mk.parole_chiave("Will the European Central Bank cut its deposit facility rate at its December 2026 meeting?") == [
        "European", "Central", "Bank"
    ]
    # Gli accenti si tolgono invece di spezzare la parola: prima "El Niño" diventava "Ni" e spariva.
    assert mk.parole_chiave("Will the August-October 2026 El Niño index be 2.10°C or higher?") == ["El", "Nino"]
    nevada = mk.parole_chiave(
        'Will "None of These Candidates" receive more votes than the winning margin in the 2026 Nevada gubernatorial election?'
    )
    assert "Nevada" in nevada and len(nevada) <= 3 and not {"None", "These", "receive", "more"} & set(nevada)
    # Un solo nome proprio: si aggiungono le parole più lunghe; senza nomi propri solo quelle, al massimo 3.
    assert mk.parole_chiave("Will Bitcoin close above $150,000 on December 31, 2026?") == ["Bitcoin", "close"]
    assert mk.parole_chiave("will the unemployment rate exceed five percent?") == ["unemployment", "rate", "five"]
    assert mk.parole_chiave("") == []


def test_senza_accenti_e_query_pulite():
    assert mk.senza_accenti("El Niño, São Paulo, Zürich, Straße, Łódź") == "El Nino, Sao Paulo, Zurich, Strasse, Lodz"
    assert mk.pulisci_query("  “El Niño” 3.4 index!! extra words ") == "El Nino 3.4 index"
    assert mk.pulisci_query(None) == "" and mk.pulisci_query(42) == ""


class _ModelloFisso:
    """Risponde sempre lo stesso testo (o solleva l'eccezione data) e ricorda i prompt."""

    def __init__(self, risposta):
        self.risposta = risposta
        self.prompt = []

    async def invoke(self, prompt):
        self.prompt.append(prompt)
        if isinstance(self.risposta, BaseException):
            raise self.risposta
        return self.risposta


def _domanda_el_nino():
    domanda = domande_di_prova()[0]
    domanda.question_text = "Will the August-October 2026 El Niño index be 2.10°C or higher?"
    domanda.resolution_criteria = "Resolves Yes if NOAA CPC reports an ASO 2026 ONI value of 2.10°C or higher."
    return domanda


async def test_query_dal_modello_pulite():
    domanda = _domanda_el_nino()
    fisso = _ModelloFisso(
        'Here you go:\n```json\n{"queries": ["El Niño", "  ENSO  ", "el nino", "Nino 3.4 index anomaly forecast", "fourth"]}\n```'
    )
    assert await mk.query_di_ricerca(domanda, fisso) == ["El Nino", "ENSO", "Nino 3.4 index anomaly"]
    assert len(fisso.prompt) == 1
    assert fisso.prompt[0].startswith("QUERY_MERCATI") and "El Niño index" in fisso.prompt[0] and "ASO 2026 ONI" in fisso.prompt[0]


async def test_query_riserva_senza_modello():
    from bot.spese import TettoSuperato

    domanda = _domanda_el_nino()
    for risposta in ("no JSON at all", '```json\n{"queries": []}\n```', '{"queries": "  "}', RuntimeError("modello giù")):
        assert await mk.query_di_ricerca(domanda, _ModelloFisso(risposta)) == ["El Nino"]
    with pytest.raises(TettoSuperato):
        await mk.query_di_ricerca(domanda, _ModelloFisso(TettoSuperato("tetto per domanda superato")))


async def test_query_costo_nel_registro(modello):
    assert await mk.query_di_ricerca(domande_di_prova()[0], modello) == ["European Central Bank", "ECB rate cut"]
    righe = modello._registro.interroga("SELECT scopo, modello, costo FROM chiamate")
    assert len(righe) == 1 and righe[0][0] == "riassunto" and righe[0][1] == "finto/claude-haiku-5-5" and righe[0][2] > 0


def _mercato(volume, prezzo=0.4, giorni=60, piattaforma="Polymarket"):
    return mk.Mercato(piattaforma, "ECB cuts rates by December?", prezzo, volume, datetime.now(timezone.utc) + timedelta(days=giorni), "https://x/y")


async def test_evidenza_mercati_pesata_e_proiettata(modello, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="bot.fonti.mercati")
    domanda = domande_di_prova()[0]  # risolve tra 60 giorni
    candidati = [_mercato(250_000, 0.4, giorni=120), _mercato(800, 0.9, piattaforma="Manifold (play money)")]

    async def cerca(query):
        assert query == ["European Central Bank", "ECB rate cut"]  # le query scritte dal modello finto
        return candidati

    testo = await mk.evidenza_mercati(domanda, modello, cerca=cerca)
    assert testo.startswith("Prediction markets")
    assert "Mercati: 2 candidati, 1 abbinati (0 same, 1 related)" in [r.getMessage() for r in caplog.records]
    # Due chiamate al modello economico, tutte e due nel registro: query e abbinamento.
    assert modello._registro.interroga("SELECT COUNT(*) FROM chiamate WHERE scopo='riassunto'")[0][0] == 2
    # Il modello finto abbina solo il candidato 1 come "related": niente proiezione (solo per "same").
    assert "match: related" in testo and "Evidence: strong (liquidity weight 1.00)" in testo
    assert "Manifold" not in testo
    riga = mk._riga_evidenza(candidati[0], "same", "", domanda.scheduled_resolution_time)
    assert "Projected to our resolution date" in riga and "40% yes" in riga
    riga_sottile = mk._riga_evidenza(candidati[1], "same", "", domanda.scheduled_resolution_time)
    assert "negligible" in riga_sottile and "Projected" not in riga_sottile


async def test_evidenza_mercati_senza_candidati_o_senza_abbinamenti(modello, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="bot.fonti.mercati")
    domanda = domande_di_prova()[0]

    async def vuota(query):
        return []

    assert await mk.evidenza_mercati(domanda, modello, cerca=vuota) is None
    assert "Mercati: nessun candidato per 'European Central Bank | ECB rate cut'" in [r.getMessage() for r in caplog.records]

    class Confuso(LlmFinto):
        async def _chiama(self, prompt):
            r = await super()._chiama(prompt)
            return r.model_copy(update={"data": "I am not sure, no JSON here"})

    confuso = Confuso(model="finto/claude-haiku-5-5", registro=modello._registro, scopo="riassunto")

    async def una(termine):
        return [_mercato(50_000)]

    testo = await mk.evidenza_mercati(domanda, confuso, cerca=una)
    assert "none judged to be on this question" in testo


def test_lettura_piattaforme(monkeypatch):
    risposte = {
        "manifold": [{"question": "ECB cut?", "probability": 0.2, "volume": 1500.5, "closeTime": 1800000000000, "outcomeType": "BINARY", "url": "https://manifold.markets/x", "isResolved": False},
                     {"question": "multi", "outcomeType": "MULTIPLE_CHOICE"}],
        "polymarket": {"events": [{"title": "ECB December", "endDate": "2026-12-20T00:00:00Z", "slug": "ecb", "volume": 9000,
                                   "markets": [{"question": "Cut in December?", "outcomePrices": "[\"0.35\", \"0.65\"]", "outcomes": "[\"Yes\", \"No\"]", "volumeNum": 120000.0, "endDate": "2026-12-18T00:00:00Z", "slug": "ecb-cut", "closed": False},
                                               {"question": "closed one", "closed": True}]}]},
    }
    parametri = {}

    def finto_get(url, **params):
        for nome, dati in risposte.items():
            if nome in url:
                parametri[nome] = params
                return dati
        raise AssertionError(url)

    monkeypatch.setattr(mk, "_get", finto_get)
    m = mk.cerca_manifold("ECB")
    assert len(m) == 1 and m[0].prezzo_si == 0.2 and m[0].chiusura.year == 2027
    # Il limite di Manifold vale prima del filtro sui binari: se ne chiedono di più.
    assert parametri["manifold"] == {"term": "ECB", "limit": 20, "filter": "open"}
    p = mk.cerca_polymarket("ECB")
    assert len(p) == 1 and p[0].prezzo_si == 0.35 and p[0].volume_usd == 120000 and p[0].url.endswith("/ecb-cut")


# --------------------------------------------------------------- Kalshi
@pytest.fixture
def kalshi_finto(monkeypatch):
    """API Kalshi finta: due pagine di eventi con il cursore e i mercati per evento. Restituisce le chiamate fatte."""
    mk.azzera_cache_kalshi()
    pagine = [
        [{"event_ticker": "KXCALCIO-1", "title": "Real Madrid vs Barcelona", "sub_title": "El Clásico"},
         {"event_ticker": "KXGOVNV-26", "title": "Nevada Governor", "sub_title": "2026"}],
        [{"event_ticker": "KXENSO-26", "title": "El Niño strength in 2026?", "sub_title": "ONI peak"}],
    ]
    mercati = {
        "KXENSO-26": [
            {"title": "El Niño strength", "yes_sub_title": "Above 2.5", "last_price": 0, "yes_bid": 10, "yes_ask": 14, "volume": 300},
            {"title": "El Niño strength", "yes_sub_title": "Above 2.0", "last_price_dollars": "0.2400", "volume_fp": "15000.00",
             "close_time": "2027-01-15T15:00:00Z"},
            {"title": "El Niño strength", "yes_sub_title": "Above 3.0", "last_price": 0, "yes_bid": 0, "yes_ask": 100, "volume": 5},
            {"title": "El Niño strength", "yes_sub_title": "Above 3.5", "last_price": 1, "volume": 1},
        ],
        "KXGOVNV-26": [{"title": "Will Joe Lombardo win the Nevada governor race?", "last_price": 62, "volume": 250000}],
    }
    chiamate = []

    def finto_get(url, **params):
        chiamate.append((url.rsplit("/", 1)[-1], params))
        if url.endswith("/events"):
            indice = int(params.get("cursor") or 0)
            return {"events": pagine[indice], "cursor": str(indice + 1) if indice + 1 < len(pagine) else ""}
        if url.endswith("/markets"):
            return {"markets": mercati.get(params["event_ticker"], [])}
        raise AssertionError(url)

    monkeypatch.setattr(mk, "_get", finto_get)
    yield chiamate
    mk.azzera_cache_kalshi()


def test_kalshi_eventi_a_pagine_e_filtro(kalshi_finto):
    import time

    k = mk.cerca_kalshi("El Nino")
    # Due pagine di eventi con il cursore, solo eventi aperti che chiudono oltre le prossime 24 ore.
    eventi = [p for nome, p in kalshi_finto if nome == "events"]
    assert len(eventi) == 2 and "cursor" not in eventi[0] and eventi[1]["cursor"] == "1"
    assert eventi[0]["status"] == "open" and eventi[0]["limit"] == 200 and eventi[0]["min_close_ts"] > time.time() + 80000
    # "El Clásico" ha solo "el": non basta. Dell'evento El Niño i 3 mercati con più volume, il più scambiato prima.
    assert [p["event_ticker"] for nome, p in kalshi_finto if nome == "markets"] == ["KXENSO-26"]
    assert [m.titolo for m in k] == ["El Niño strength Above 2.0", "El Niño strength Above 2.5", "El Niño strength Above 3.0"]
    assert k[0].prezzo_si == pytest.approx(0.24) and k[0].volume_usd == 15000 and k[0].chiusura.year == 2027
    assert k[1].prezzo_si == pytest.approx(0.12)  # mai scambiato: punto medio della forbice stretta
    assert k[2].prezzo_si is None  # forbice 0-100: nessun prezzo
    assert k[0].url == "https://kalshi.com/markets/KXENSO-26"
    # La lista degli eventi si scarica una volta sola per il giro.
    g = mk.cerca_kalshi("Nevada governor")
    assert len([1 for nome, _ in kalshi_finto if nome == "events"]) == 2
    assert len(g) == 1 and g[0].prezzo_si == pytest.approx(0.62) and g[0].volume_usd == 250000
    assert mk.cerca_kalshi("Swiss drought") == [] and mk.cerca_kalshi("the will") == []


def test_kalshi_prezzi_e_punteggio():
    assert mk.prezzo_kalshi({"last_price_dollars": "0.3100", "last_price": 99}) == pytest.approx(0.31)
    assert mk.prezzo_kalshi({"last_price": 31}) == pytest.approx(0.31)
    assert mk.prezzo_kalshi({"last_price": 0, "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44"}) == pytest.approx(0.42)
    assert mk.prezzo_kalshi({"last_price": 0, "yes_bid": 20, "yes_ask": 60}) is None
    assert mk.prezzo_kalshi({"last_price_dollars": "31"}) is None and mk.prezzo_kalshi({}) is None
    evento = {"title": "Nevada Governor Election", "sub_title": "Winner"}
    assert mk.punteggio_evento(["nevada", "governor"], evento) == 1.0
    assert mk.punteggio_evento(["nevada", "senate"], evento) == 0.0
    assert mk.punteggio_evento(["nevada", "governor", "lombardo"], evento) == pytest.approx(2 / 3)
    assert mk.punteggio_evento(["nevada", "candidates", "lombardo"], evento) == 0.0


def test_kalshi_guasto_non_riprova_subito(monkeypatch):
    mk.azzera_cache_kalshi()
    chiamate = []

    def rotto(url, **params):
        chiamate.append(url)
        raise ConnectionError("kalshi giù")

    monkeypatch.setattr(mk, "_get", rotto)
    with pytest.raises(ConnectionError):
        mk.cerca_kalshi("El Nino")
    assert mk.cerca_kalshi("Nevada governor") == [] and len(chiamate) == 1
    mk.azzera_cache_kalshi()


def test_kalshi_filtro_chiusura_rifiutato(monkeypatch):
    import requests

    mk.azzera_cache_kalshi()
    chiamate = []

    def finto_get(url, **params):
        chiamate.append(dict(params))
        if url.endswith("/events"):
            if "min_close_ts" in params:
                risposta = requests.Response()
                risposta.status_code = 400
                raise requests.HTTPError("400 Bad Request", response=risposta)
            return {"events": [{"event_ticker": "KXENSO-26", "title": "El Nino in 2026?"}], "cursor": ""}
        return {"markets": [{"title": "El Nino in 2026?", "last_price": 40, "volume": 2000}]}

    monkeypatch.setattr(mk, "_get", finto_get)
    k = mk.cerca_kalshi("El Nino")
    assert len(k) == 1 and k[0].prezzo_si == pytest.approx(0.40)
    assert "min_close_ts" in chiamate[0] and "min_close_ts" not in chiamate[1]
    mk.azzera_cache_kalshi()


def test_interruttore_kalshi(monkeypatch):
    from bot.config import Impostazioni

    monkeypatch.delenv("KALSHI", raising=False)
    assert Impostazioni.da_ambiente().kalshi is True
    monkeypatch.setenv("KALSHI", "no")
    assert Impostazioni.da_ambiente().kalshi is False
    assert list(mk.piattaforme_attive()) == ["Manifold", "Polymarket", "Kalshi"]
    assert list(mk.piattaforme_attive(kalshi=False)) == ["Manifold", "Polymarket"]


async def test_ricercatore_usa_le_piattaforme_attive(monkeypatch, modello):
    from bot.config import Impostazioni
    from bot.ricerca import Ricercatore

    viste = []

    async def finta(query, piattaforme=None):
        viste.append((query, list(piattaforme)))
        return []

    monkeypatch.setattr(mk, "cerca_tutti", finta)
    r = Ricercatore(Impostazioni(kalshi=False), modello._registro, modello, modello, cerca_web=None, cerca_notizie=None)
    assert await r._mercati(domande_di_prova()[0]) is None
    assert viste == [(["European Central Bank", "ECB rate cut"], ["Manifold", "Polymarket"])]


async def test_cerca_tutti_piu_query_senza_doppioni(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="bot.fonti.mercati")

    def piattaforma(nome):
        def cerca(termine):
            comune = mk.Mercato(nome, "Comune", 0.5, 2000, None, f"https://{nome}/comune")
            return [comune] + [mk.Mercato(nome, f"{termine} {i}", 0.5, 2000, None, f"https://{nome}/{termine}/{i}") for i in range(4)]

        return cerca

    trovati = await mk.cerca_tutti(["El Nino", "ENSO"], {"A": piattaforma("A"), "B": piattaforma("B")})
    # Al massimo 5 per piattaforma, a turno tra le query, il mercato trovato da tutte e due una volta sola.
    assert [m.titolo for m in trovati if m.piattaforma == "A"] == ["Comune", "El Nino 0", "ENSO 0", "El Nino 1", "ENSO 1"]
    assert len(trovati) == 10
    assert "Mercati per 'El Nino | ENSO': 10 trovati (A 5, B 5)" in [r.getMessage() for r in caplog.records]


async def test_cerca_tutti_una_piattaforma_guasta(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="bot.fonti.mercati")

    def ok(termine):
        return [_mercato(1000)]

    def rotta(termine):
        raise ConnectionError

    trovati = await mk.cerca_tutti("x", {"A": ok, "B": rotta})
    assert len(trovati) == 1
    assert "Mercati per 'x': 1 trovati (A 1, B 0)" in [r.getMessage() for r in caplog.records]
    # Con più query la piattaforma guasta lascia un avviso solo, non uno per query.
    caplog.clear()
    await mk.cerca_tutti(["x", "y", "z"], {"A": ok, "B": rotta})
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == ["Mercati B non letti: ConnectionError: "]


# ---------------------------------------------------------- serie storiche
def _serie(n=200, passo=1.0):
    inizio = date(2026, 1, 1)
    return [(inizio + timedelta(days=i), 100 + passo * (i % 7) + i * 0.1) for i in range(n)]


def test_variazioni_su_finestre_e_descrizione():
    lineare = _serie(passo=0.0)
    variazioni = ss.variazioni_su_finestre(lineare, 30)
    assert len(variazioni) == 170 and all(v == pytest.approx(3.0, abs=1e-6) for v in variazioni)
    serie = _serie()
    testo = ss.descrivi(serie, "Yahoo Finance BZ=F", 30, "USD")
    assert "latest value" in testo and "2026-07-19" in testo and "30-day windows (n=170)" in testo and "random-walk width" in testo
    assert "Too few" in ss.descrivi(serie[:3], "x", 30)
    assert ss.descrivi([], "x", 30) == "x: no data."


async def test_serie_storiche_yahoo_senza_fred(modello, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    domanda = domande_di_prova()[1]
    chiamate = []

    def yahoo(ticker):
        chiamate.append(ticker)
        return _serie()

    def fred(serie, chiave):
        raise AssertionError("FRED non va chiamata senza chiave")

    testo = await ss.serie_storiche(domanda, modello, scarica_fred_=fred, scarica_yahoo_=yahoo)
    assert chiamate == ["BZ=F"] and "Historical data series" in testo and "Yahoo Finance BZ=F" in testo
    assert "Brent front-month future" in testo


async def test_serie_storiche_fred_csv_senza_chiave(modello, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    class ConFred(LlmFinto):
        async def _chiama(self, prompt):
            r = await super()._chiama(prompt)
            assert "FRED series id" in prompt  # FRED si chiede sempre: senza chiave c'è il CSV pubblico
            return r.model_copy(update={"data": '{"fred_series_id": "DCOILBRENTEU", "yahoo_ticker": null}'})

    m = ConFred(model="finto/claude-haiku-5-5", registro=modello._registro, scopo="riassunto")
    chiamate = []

    def csv_(serie):
        chiamate.append(serie)
        return _serie(50)

    def api(serie, chiave):
        raise AssertionError("l'API FRED non va chiamata senza chiave")

    testo = await ss.serie_storiche(domande_di_prova()[1], m, scarica_fred_=api, scarica_fred_csv_=csv_)
    assert chiamate == ["DCOILBRENTEU"] and "FRED series DCOILBRENTEU (public CSV)" in testo and "latest value" in testo


async def test_serie_storiche_fred_con_chiave_e_guasti(modello, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "chiave-di-prova")

    class ConFred(LlmFinto):
        async def _chiama(self, prompt):
            r = await super()._chiama(prompt)
            return r.model_copy(update={"data": '{"fred_series_id": "DCOILBRENTEU", "yahoo_ticker": "BZ=F"}'})

    m = ConFred(model="finto/claude-haiku-5-5", registro=modello._registro, scopo="riassunto")
    visti = {}

    def fred(serie, chiave):
        visti["fred"] = (serie, chiave)
        return _serie(50)

    def yahoo(ticker):
        raise TimeoutError

    testo = await ss.serie_storiche(domande_di_prova()[1], m, scarica_fred_=fred, scarica_yahoo_=yahoo)
    assert visti["fred"] == ("DCOILBRENTEU", "chiave-di-prova")
    assert "FRED series DCOILBRENTEU" in testo and "Yahoo" not in testo
    assert "chiave-di-prova" not in testo  # la chiave non finisce mai nel briefing

    def tutto_rotto(*a):
        raise ConnectionError

    assert await ss.serie_storiche(domande_di_prova()[1], m, scarica_fred_=tutto_rotto, scarica_yahoo_=tutto_rotto) is None


def test_pulisci_id():
    assert ss._pulisci_id(" CPIAUCSL ") == "CPIAUCSL" and ss._pulisci_id("null") is None
    assert ss._pulisci_id("not a ticker") is None and ss._pulisci_id(12) is None
