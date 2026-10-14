import pytest

from bot.spese import Registro, TettoSuperato, domanda_corrente, stima_costo


@pytest.fixture
def registro(tmp_path):
    r = Registro(tmp_path / "r.sqlite", tetto_domanda=0.5, tetto_giorno=2.0, tetto_mese=5.0)
    yield r
    r.chiudi()


def test_stima_costo_listino():
    # Opus 5.5: 4 $/M in, 20 $/M out
    assert stima_costo("anthropic/claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert stima_costo("openrouter/anthropic/claude-opus-5.5", 0, 1_000_000) == pytest.approx(20.0)
    assert stima_costo("anthropic/claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.6)
    # 10 $ ogni 1.000 ricerche web
    assert stima_costo("anthropic/claude-sonnet-5-5", 0, 0, ricerche_web=3) == pytest.approx(0.03)


def test_costo_zero_esplicito_resta_zero(registro):
    assert registro.registra_chiamata("asknews/news", 0, 0, costo=0.0, ricerche_web=2) == 0.0


def test_costo_sconosciuto_stimato(registro):
    assert registro.registra_chiamata("anthropic/claude-opus-5-5", 1000, 1000) == pytest.approx(0.024)


def test_tetto_domanda(registro):
    registro.controlla_tetti("d1")
    registro.registra_chiamata("x", 0, 0, costo=0.5, domanda="d1")
    with pytest.raises(TettoSuperato, match="per domanda"):
        registro.controlla_tetti("d1")
    registro.controlla_tetti("d2")  # le altre domande vanno avanti


def test_tetto_giorno(registro):
    for i in range(5):
        registro.registra_chiamata("x", 0, 0, costo=0.4, domanda=f"d{i}")
    with pytest.raises(TettoSuperato, match="giornaliero"):
        registro.controlla_tetti("nuova")


def test_tetto_mese(tmp_path):
    r = Registro(tmp_path / "m.sqlite", tetto_domanda=100, tetto_giorno=100, tetto_mese=1.0)
    r.registra_chiamata("x", 0, 0, costo=1.0)
    with pytest.raises(TettoSuperato, match="mensile"):
        r.controlla_tetti(None)
    r.chiudi()


def test_domanda_dal_contesto(registro):
    segno = domanda_corrente.set("dx")
    try:
        registro.registra_chiamata("x", 0, 0, costo=0.1)
    finally:
        domanda_corrente.reset(segno)
    assert registro.spesa_domanda("dx") == pytest.approx(0.1)


def test_previsioni_e_gia_prevista(registro):
    registro.registra_chiamata("x", 0, 0, costo=0.2, domanda="d1")
    registro.registra_previsione("d1", "u", "t", "minibench", "binary", 0.3, inviata=False)
    registro.registra_previsione("d2", "u", "t", "minibench", "binary", None, inviata=False, errore="boom")
    assert registro.gia_prevista("d1", inviata=False)
    assert not registro.gia_prevista("d1", inviata=True)
    assert not registro.gia_prevista("d2", inviata=False)  # gli errori si riprovano
    costo = registro.interroga("SELECT costo FROM previsioni WHERE domanda='d1'")[0][0]
    assert costo == pytest.approx(0.2)


def test_rapporto_senza_token(tmp_path, monkeypatch):
    from bot.config import Impostazioni
    from bot.rapporto import scrivi_rapporto

    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    imp = Impostazioni(percorso_registro=tmp_path / "r.sqlite")
    r = Registro(imp.percorso_registro, 1, 1, 1)
    r.registra_chiamata("anthropic/claude-opus-5-5", 1000, 1000, domanda="d1")
    r.registra_previsione("d1", "u", "t", "minibench", "binary", 0.3, inviata=False)
    r.registra_previsione("d2", "u", "t", "minibench", "binary", None, inviata=False, errore="TettoSuperato: x")
    r.chiudi()
    testo = scrivi_rapporto(imp, cartella=tmp_path / "rapporti").read_text(encoding="utf-8")
    assert "minibench: 1 previste (0 inviate), 1 saltate" in testo
    assert "Manca METACULUS_TOKEN" in testo


def test_stima_premio():
    from bot.rapporto import stima_premio

    assert stima_premio(10, [10, 10], 1000) == 500
    assert stima_premio(1, [1, 10], 1000) == 0  # sotto i 50 $ non si paga
    assert stima_premio(-5, [10], 1000) == 0
