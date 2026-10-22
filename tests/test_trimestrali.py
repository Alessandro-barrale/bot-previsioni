"""Modulo trimestrali (Market Pulse) con risposte finte di SEC EDGAR e Yahoo Finance: nessuna rete."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import requests

from bot.fonti import trimestrali as tr
from bot.llm import LlmFinto
from bot.spese import Registro


def _domanda(testo, etichetta=None, titolo_gruppo=None):
    return SimpleNamespace(question_text=testo, group_question_option=etichetta, api_json={"title": titolo_gruppo} if titolo_gruppo else {})


# ------------------------------------------------------------ azienda
def test_trova_azienda_dal_nome():
    assert tr.trova_azienda(_domanda("What will Nvidia's revenue be in fiscal Q3 2027?")) == ("nvidia", "NVDA")
    assert tr.trova_azienda(_domanda("Meta Platforms Q3 2026 revenue")) == ("meta platforms", "META")
    assert tr.trova_azienda(_domanda("AMD data center revenue")) == ("amd", "AMD")
    # L'etichetta della sotto-domanda viene prima del testo e del titolo del gruppo.
    assert tr.trova_azienda(_domanda("Q3 2026 revenue, by company", etichetta="Tesla", titolo_gruppo="Apple and peers")) == ("tesla", "TSLA")
    assert tr.trova_azienda(_domanda("Revenue", titolo_gruppo="Amazon Q3 2026 results")) == ("amazon", "AMZN")
    # Parole intere: "metadata" non è Meta, "intelligence" non è Intel.
    assert tr.trova_azienda(_domanda("Will the metadata standard for artificial intelligence pass?")) is None
    assert tr.trova_azienda(_domanda("SpaceX Starlink revenue in 2026")) is None


class ModelloSenzaTicker:
    async def invoke(self, prompt):
        return '```json\n{"company": "SpaceX", "ticker": null}\n```'


async def test_identifica_azienda_con_il_modello_solo_se_serve(tmp_path):
    registro = Registro(tmp_path / "r.sqlite", 5, 50, 500)
    modello = LlmFinto(model="finto/claude-haiku-5-5", registro=registro, scopo="riassunto")
    assert await tr.identifica_azienda(_domanda("Nvidia revenue"), modello) == ("nvidia", "NVDA")
    assert registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0] == 0  # elenco: nessuna chiamata
    assert await tr.identifica_azienda(_domanda("Example Corp revenue"), modello) == ("Example Corp", "EXMP")
    assert registro.interroga("SELECT COUNT(*) FROM chiamate")[0][0] == 1
    assert await tr.identifica_azienda(_domanda("SpaceX Starlink revenue"), ModelloSenzaTicker()) is None
    assert await tr.identifica_azienda(_domanda("SpaceX Starlink revenue"), None) is None
    registro.chiudi()


# ------------------------------------------------------------ SEC EDGAR
def _fatto(inizio, fine, valore, form="10-Q", depositato="2026-01-01"):
    return {"start": inizio, "end": fine, "val": valore, "form": form, "filed": depositato}


FATTI_RICAVI = [
    _fatto("2025-01-01", "2025-03-31", 100, depositato="2025-05-01"),
    _fatto("2025-04-01", "2025-06-30", 110, depositato="2025-08-01"),
    _fatto("2025-07-01", "2025-09-30", 120, depositato="2025-11-01"),
    _fatto("2025-01-01", "2025-12-31", 470, form="10-K", depositato="2026-02-01"),  # anno: Q4 = 470 - 330
    _fatto("2026-01-01", "2026-03-31", 125, depositato="2026-05-01"),
    _fatto("2026-01-01", "2026-03-31", 126, form="10-Q/A", depositato="2026-06-15"),  # rettifica: vince l'ultima
    _fatto("2025-01-01", "2025-03-31", 100, depositato="2026-05-01"),  # comparativo ripetuto
    _fatto("2026-04-01", "2026-06-30", 135, depositato="2026-08-01"),
    _fatto("2026-01-01", "2026-06-30", 261, depositato="2026-08-01"),  # semestre: ignorato
    _fatto("2026-07-01", "2026-09-30", 150, depositato="2026-10-20"),
    _fatto("2026-07-01", "2026-09-30", 999, form="8-K", depositato="2026-10-21"),  # non è un 10-Q/10-K
]


def test_trimestri_da_fatti_con_quarto_trimestre_ricavato():
    trimestri = tr.trimestri_da_fatti(FATTI_RICAVI)
    assert [(t.fine.isoformat(), t.valore, t.derivato) for t in trimestri] == [
        ("2025-03-31", 100, False), ("2025-06-30", 110, False), ("2025-09-30", 120, False),
        ("2025-12-31", 140, True), ("2026-03-31", 126, False), ("2026-06-30", 135, False), ("2026-09-30", 150, False),
    ]
    assert trimestri[3].inizio == date(2025, 10, 1)


def test_dati_sec_testo_con_crescite():
    def scarica(cik, concetto):
        assert cik == 1045810
        if concetto == "Revenues":
            risposta = requests.Response()
            risposta.status_code = 404
            raise requests.HTTPError("404", response=risposta)
        if concetto == "RevenueFromContractWithCustomerExcludingAssessedTax":
            return {"units": {"USD": [dict(f, val=f["val"] * 1e9) for f in FATTI_RICAVI]}}
        if concetto == "EarningsPerShareDiluted":
            return {"units": {"USD/shares": [_fatto("2026-07-01", "2026-09-30", 1.25), _fatto("2025-07-01", "2025-09-30", 1.0)]}}
        return {"units": {}}

    testo = tr.dati_sec("NVDA", scarica=scarica, cik_di=lambda ticker: 1045810)
    assert "Revenue (XBRL concept RevenueFromContractWithCustomerExcludingAssessedTax):" in testo
    assert "- quarter ending 2026-09-30: 150.00 B USD (y/y +25.0%, q/q +11.1%)" in testo
    assert "- quarter ending 2025-12-31: 140.00 B USD (q/q +16.7%, derived: fiscal year minus Q1-Q3)" in testo
    assert "Sequential (q/q) growth over these quarters" in testo
    assert "- quarter ending 2026-09-30: 1.25 USD per share (y/y +25.0%)" in testo
    assert "Net income" not in testo  # concetto senza dati: niente righe


# ------------------------------------------------------------ Yahoo Finance
SINTESI = {
    "earningsTrend": {"trend": [
        {"period": "0q", "endDate": "2026-10-31",
         "earningsEstimate": {"avg": {"raw": 1.25, "fmt": "1.25"}, "low": {"raw": 1.18}, "high": {"raw": 1.31},
                              "numberOfAnalysts": {"raw": 41}, "yearAgoEps": {"raw": 0.81}, "growth": {"raw": 0.543}},
         "revenueEstimate": {"avg": {"raw": 54.6e9}, "low": {"raw": 53.1e9}, "high": {"raw": 56.9e9},
                             "numberOfAnalysts": {"raw": 45}, "yearAgoRevenue": {"raw": 35.08e9}, "growth": {"raw": 0.556}},
         "epsTrend": {"current": {"raw": 1.25}, "90daysAgo": {"raw": 1.14}}},
        {"period": "+1q", "endDate": "2027-01-31", "revenueEstimate": {"avg": {"raw": 60.1e9}}, "earningsEstimate": {}},
        {"period": "+5y", "growth": {"raw": 0.3}},
    ]},
    "earningsHistory": {"history": [
        {"quarter": {"raw": 1753920000, "fmt": "2025-07-31"}, "epsActual": {"raw": 1.05}, "epsEstimate": {"raw": 1.01},
         "surprisePercent": {"raw": 0.0396}},
        {"quarter": {"fmt": "2025-10-31"}, "epsActual": {"raw": 1.30}, "epsEstimate": {"raw": 1.25}, "surprisePercent": {}},
        {"quarter": {"fmt": "2026-01-31"}, "epsActual": {}, "epsEstimate": {"raw": 1.4}},
    ]},
    "calendarEvents": {"earnings": {"earningsDate": [{"raw": 1795000000, "fmt": "2026-11-19"}], "revenueAverage": {"raw": 54.6e9}}},
}


def test_descrivi_sintesi_consenso_sorprese_e_data():
    righe = tr.descrivi_sintesi(SINTESI)
    assert righe[0] == (
        "- current quarter (period ending 2026-10-31): revenue consensus 54.60 B (low 53.10 B, high 56.90 B, 45 analysts, "
        "year-ago 35.08 B, growth +55.6%); EPS consensus 1.25 (low 1.18, high 1.31, 41 analysts, year-ago 0.81, "
        "growth +54.3%); EPS consensus 90 days ago 1.14"
    )
    assert righe[1] == "- next quarter (period ending 2027-01-31): revenue consensus 60.10 B"
    assert righe[2] == "- EPS actual vs consensus, last quarters: 2025-07-31: 1.05 vs 1.01 (+4.0%); 2025-10-31: 1.30 vs 1.25 (+4.0%)"
    assert righe[3].startswith("  EPS surprise: mean +4.0%") and righe[3].endswith("beats 2 of 2.")
    assert righe[4] == "- Next earnings report date (Yahoo): 2026-11-19"
    assert len(righe) == 5  # il consenso sui ricavi del calendario non si ripete
    assert tr.descrivi_sintesi({}) == []
    solo_calendario = tr.descrivi_sintesi({"calendarEvents": SINTESI["calendarEvents"]})
    assert solo_calendario[-1] == "- Revenue consensus for the next report (Yahoo calendar): 54.60 B"


def test_prezzo_yahoo_ultimo_e_tre_mesi():
    inizio = date(2026, 4, 1)
    serie = [(inizio + timedelta(days=i), 100.0 + i) for i in range(180)]
    testo = tr.prezzo_yahoo("NVDA", scarica=lambda ticker, intervallo: serie)
    ultimo = serie[-1]
    assert testo.startswith(f"Stock NVDA: last close {ultimo[1]:,.2f} on {ultimo[0].isoformat()}, 3-month change +")
    assert "context only" in testo


# ------------------------------------------------------------ orchestrazione
async def test_trimestrali_un_pezzo_guasto_non_blocca():
    def sec_guasta(ticker):
        raise ConnectionError("SEC giù")

    testo = await tr.trimestrali(
        _domanda("Nvidia revenue fiscal Q3 2027"), None,
        sec=sec_guasta, consenso=lambda t: "Analyst consensus: revenue 54.6 B", prezzo=lambda t: "Stock NVDA: last close 190",
    )
    assert testo.startswith("Structured quarterly data for ticker NVDA (company: nvidia)")
    assert "revenue surprise history and guidance are not in these free feeds" in testo
    assert "Analyst consensus: revenue 54.6 B" in testo and "Stock NVDA" in testo

    def guasta(ticker):
        raise ConnectionError("giù")

    assert await tr.trimestrali(_domanda("Nvidia revenue"), None, sec=guasta, consenso=guasta, prezzo=guasta) is None
    assert await tr.trimestrali(_domanda("SpaceX Starlink revenue"), None, sec=guasta, consenso=guasta, prezzo=guasta) is None


def test_user_agent_sec_senza_indirizzi_personali(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert tr._intestazioni_sec()["User-Agent"] == "bot-previsioni research-bot@example.com"
    monkeypatch.setenv("SEC_USER_AGENT", "altro agente")
    assert tr._intestazioni_sec()["User-Agent"] == "altro agente"


@pytest.mark.parametrize("valore, atteso", [({"raw": 1.5, "fmt": "1.50"}, 1.5), (2, 2.0), ({}, None), (None, None), (True, None)])
def test_numeri_di_yahoo(valore, atteso):
    assert tr._num(valore) == atteso
