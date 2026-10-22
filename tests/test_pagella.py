"""Pagella con dati finti nel formato dell'API di Metaculus (nessuna rete)."""
import math

import pytest

from bot import pagella

CHIUSURA = "2026-10-20T12:00:00Z"
T_CHIUSURA = 1_792_497_600  # 2026-10-20T12:00:00Z


def _post(domanda: dict, post_id: int = 100, torneo: str = "minibench") -> dict:
    return {"id": post_id, "title": domanda.get("title", "t"), "projects": {"default_project": {"slug": torneo}}, "question": domanda}


def _storia(valori_prima, valori_dopo=None):
    storia = [{"start_time": T_CHIUSURA - 3600, "end_time": T_CHIUSURA, "forecast_values": valori_prima}]
    if valori_dopo is not None:  # una voce DOPO la chiusura non deve contare
        storia.append({"start_time": T_CHIUSURA + 60, "end_time": None, "forecast_values": valori_dopo})
    return storia


def _binaria(nostra=0.8, community=0.6, risoluzione="yes"):
    return {
        "id": 7, "title": "Will X happen?", "type": "binary", "resolution": risoluzione,
        "actual_close_time": CHIUSURA, "actual_resolve_time": "2026-10-21T00:00:00Z",
        "my_forecasts": {"history": _storia([1 - nostra, nostra], [0.01, 0.99])},
        "aggregations": {"recency_weighted": {"history": _storia([1 - community, community], [0.0, 1.0])}},
    }


def test_binaria_meglio_della_community():
    riga = pagella.valuta_domanda(_post(_binaria()), _binaria())
    assert riga is not None
    assert riga.log_nostro == pytest.approx(math.log(0.8))
    assert riga.log_community == pytest.approx(math.log(0.6))
    assert riga.peer_stimato == pytest.approx(100 * (math.log(0.8) - math.log(0.6)))
    assert riga.torneo == "minibench" and riga.domanda == "100-7" and riga.tipo == "binary"


def test_binaria_risolta_no_usa_il_complemento():
    d = _binaria(nostra=0.8, community=0.6, risoluzione="no")
    riga = pagella.valuta_domanda(_post(d), d)
    assert riga.log_nostro == pytest.approx(math.log(0.2))
    assert riga.peer_stimato < 0


def test_annullata_o_senza_previsione_o_senza_community():
    d = _binaria(risoluzione="annulled")
    assert pagella.valuta_domanda(_post(d), d) is None
    d = _binaria()
    d["my_forecasts"] = {"history": [{"start_time": T_CHIUSURA + 10, "end_time": None, "forecast_values": [0.5, 0.5]}]}
    assert pagella.valuta_domanda(_post(d), d) is None
    d = _binaria()
    d["aggregations"] = {}
    assert pagella.valuta_domanda(_post(d), d) is None


def test_community_dai_centers_se_mancano_i_valori():
    d = _binaria()
    d["aggregations"] = {"recency_weighted": {"history": [{"start_time": T_CHIUSURA - 10, "end_time": None, "centers": [0.6]}]}}
    riga = pagella.valuta_domanda(_post(d), d)
    assert riga.log_community == pytest.approx(math.log(0.6))


def test_scelta_multipla():
    d = {
        "id": 8, "title": "Who?", "type": "multiple_choice", "options": ["A", "B", "C"], "resolution": "B",
        "actual_close_time": CHIUSURA,
        "my_forecasts": {"history": _storia([0.2, 0.5, 0.3])},
        "aggregations": {"unweighted": {"history": _storia([0.6, 0.2, 0.2])}},
    }
    riga = pagella.valuta_domanda(_post(d), d)
    assert riga.log_nostro == pytest.approx(math.log(0.5))
    assert riga.log_community == pytest.approx(math.log(0.2))
    assert "max: A" in riga.community


def _cdf(centro: float, larghezza: float, punti: int = 201) -> list[float]:
    # CDF logistica su [0, 1], con un po' di massa fuori dai limiti
    import math as m

    return [0.02 + 0.96 / (1 + m.exp(-(i / (punti - 1) - centro) / larghezza)) for i in range(punti)]


def test_numerica_massa_del_bin_e_fuori_scala():
    scaling = {"range_min": 0.0, "range_max": 100.0, "zero_point": None}
    stretta = _cdf(0.5, 0.02)
    larga = _cdf(0.5, 0.2)
    # Esito 50: la CDF stretta mette più massa nel bin centrale.
    assert pagella.massa_del_bin(stretta, "50", "numeric", scaling) > pagella.massa_del_bin(larga, "50", "numeric", scaling)
    # Esito 95: la larga va meglio.
    assert pagella.massa_del_bin(larga, "95", "numeric", scaling) > pagella.massa_del_bin(stretta, "95", "numeric", scaling)
    # Fuori scala: la massa oltre il limite.
    assert pagella.massa_del_bin(larga, "above_upper_bound", "numeric", scaling) == pytest.approx(1 - larga[-1])
    assert pagella.massa_del_bin(larga, "-5", "numeric", scaling) == pytest.approx(larga[0])


def test_numerica_scala_logaritmica_e_data():
    scaling_log = {"range_min": 1.0, "range_max": 1000.0, "zero_point": 0.0}
    assert pagella._posizione(1.0, scaling_log) == pytest.approx(0.0)
    assert pagella._posizione(1000.0, scaling_log) == pytest.approx(1.0)
    assert pagella._posizione(31.6227766, scaling_log) == pytest.approx(0.5, abs=1e-6)
    scaling_data = {"range_min": T_CHIUSURA, "range_max": T_CHIUSURA + 100 * 86400, "zero_point": None}
    posizione = pagella._posizione(pagella._valore_risoluzione("2026-11-09T12:00:00Z", "date"), scaling_data)
    assert posizione == pytest.approx(0.2)


def test_numerica_riga_completa_e_gruppo():
    scaling = {"range_min": 0.0, "range_max": 100.0, "zero_point": None}
    sotto = {
        "id": 9, "title": "How many?", "type": "numeric", "resolution": "52.0", "scaling": scaling,
        "actual_close_time": CHIUSURA,
        "my_forecasts": {"history": _storia(_cdf(0.5, 0.05))},
        "aggregations": {"recency_weighted": {"history": _storia(_cdf(0.3, 0.05))}},
    }
    post = {"id": 200, "title": "Group", "projects": {"default_project": {"slug": "fall-futureeval-2026"}}, "group_of_questions": {"questions": [sotto]}}
    righe = pagella.valuta_post(post)
    assert len(righe) == 1
    assert righe[0].peer_stimato > 0 and righe[0].torneo == "fall-futureeval-2026" and righe[0].domanda == "200-9"


def test_salvataggio_riepilogo_e_tabella(tmp_path):
    percorso = tmp_path / "pagella.sqlite"
    assert "Nessuna domanda risolta" in pagella.tabella_markdown(percorso)[0]
    d1 = _binaria()
    d2 = _binaria(nostra=0.3, community=0.6, risoluzione="yes")
    d2["id"] = 8
    righe = [pagella.valuta_domanda(_post(d1), d1), pagella.valuta_domanda(_post(d2, post_id=101, torneo="fall-futureeval-2026"), d2)]
    assert pagella.salva_righe(righe, percorso) == 2
    # Riscrivere la stessa domanda aggiorna la riga, non la duplica.
    assert pagella.salva_righe(righe[:1], percorso) == 1
    dati = pagella.riepilogo(percorso)
    assert dati["totale"]["n"] == 2 and dati["totale"]["vinte"] == 1
    assert {t["torneo"] for t in dati["tornei"]} == {"minibench", "fall-futureeval-2026"}
    testo = "\n".join(pagella.tabella_markdown(percorso))
    assert "| 2026-10-20 | minibench | binary | Will X happen? | yes |" in testo
    assert "battiamo la community in 1 su 1" in testo


def test_rapporto_include_la_pagella(tmp_path, monkeypatch):
    from bot.config import Impostazioni
    from bot.rapporto import scrivi_rapporto

    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    percorso = tmp_path / "pagella.sqlite"
    d = _binaria()
    pagella.salva_righe([pagella.valuta_domanda(_post(d), d)], percorso)
    imp = Impostazioni(percorso_registro=tmp_path / "r.sqlite")
    testo = scrivi_rapporto(imp, cartella=tmp_path / "rapporti", percorso_pagella=percorso).read_text(encoding="utf-8")
    assert "## Pagella" in testo and "spot peer stimato medio +28.8" in testo
