"""Fonti v2 (fonte di risoluzione, mercati, serie storiche) con risposte finte: nessuna rete."""
from datetime import date, datetime, timedelta, timezone

import pytest

from bot.domande_prova import domande_di_prova
from bot.fonti import fonte_risoluzione as fr
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
    chiavi = mk.parole_chiave("Will the European Central Bank cut its deposit facility rate at its December 2026 meeting?")
    assert chiavi[:3] == ["european", "central", "bank"] and "will" not in chiavi and len(chiavi) <= 6


def _mercato(volume, prezzo=0.4, giorni=60, piattaforma="Polymarket"):
    return mk.Mercato(piattaforma, "ECB cuts rates by December?", prezzo, volume, datetime.now(timezone.utc) + timedelta(days=giorni), "https://x/y")


async def test_evidenza_mercati_pesata_e_proiettata(modello):
    domanda = domande_di_prova()[0]  # risolve tra 60 giorni
    candidati = [_mercato(250_000, 0.4, giorni=120), _mercato(800, 0.9, piattaforma="Manifold (play money)")]

    async def cerca(termine):
        assert "european" in termine
        return candidati

    testo = await mk.evidenza_mercati(domanda, modello, cerca=cerca)
    assert testo.startswith("Prediction markets")
    # Il modello finto abbina solo il candidato 1 come "related": niente proiezione (solo per "same").
    assert "match: related" in testo and "Evidence: strong (liquidity weight 1.00)" in testo
    assert "Manifold" not in testo
    riga = mk._riga_evidenza(candidati[0], "same", "", domanda.scheduled_resolution_time)
    assert "Projected to our resolution date" in riga and "40% yes" in riga
    riga_sottile = mk._riga_evidenza(candidati[1], "same", "", domanda.scheduled_resolution_time)
    assert "negligible" in riga_sottile and "Projected" not in riga_sottile


async def test_evidenza_mercati_senza_candidati_o_senza_abbinamenti(modello):
    domanda = domande_di_prova()[0]

    async def vuota(termine):
        return []

    assert await mk.evidenza_mercati(domanda, modello, cerca=vuota) is None

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
        "kalshi": {"markets": [{"ticker": "ECB-26DEC", "event_ticker": "ECB", "title": "European Central Bank rate decision", "yes_sub_title": "Cut", "last_price": 31, "volume": 4200, "close_time": "2026-12-18T12:00:00Z"},
                               {"ticker": "X", "title": "Something else", "last_price": 50, "volume": 10}]},
    }

    def finto_get(url, **params):
        for nome, dati in risposte.items():
            if nome in url:
                return dati
        raise AssertionError(url)

    monkeypatch.setattr(mk, "_get", finto_get)
    m = mk.cerca_manifold("ECB")
    assert len(m) == 1 and m[0].prezzo_si == 0.2 and m[0].chiusura.year == 2027
    p = mk.cerca_polymarket("ECB")
    assert len(p) == 1 and p[0].prezzo_si == 0.35 and p[0].volume_usd == 120000 and p[0].url.endswith("/ecb-cut")
    k = mk.cerca_kalshi("European Central Bank cut December")
    assert len(k) == 1 and k[0].prezzo_si == 0.31 and k[0].volume_usd == 4200


async def test_cerca_tutti_una_piattaforma_guasta():
    def ok(termine):
        return [_mercato(1000)]

    def rotta(termine):
        raise ConnectionError

    trovati = await mk.cerca_tutti("x", {"A": ok, "B": rotta})
    assert len(trovati) == 1


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
