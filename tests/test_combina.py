from datetime import datetime, timedelta, timezone

import pytest
from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion, Percentile

from bot import combina


# ------------------------------------------------------------ sì/no
def test_legge_ultima_probabilita():
    testo = "Prima pensavo Probability: 80%\n...\nProbability: 35%"
    assert combina.leggi_probabilita(testo) == pytest.approx(0.35)


def test_probabilita_con_decimali_e_grassetto():
    assert combina.leggi_probabilita("**Probability: 12.5%**") == pytest.approx(0.125)


def test_probabilita_limitata_tra_1_e_99():
    assert combina.leggi_probabilita("Probability: 0%") == 0.01
    assert combina.leggi_probabilita("Probability: 100%") == 0.99


def test_probabilita_mancante():
    with pytest.raises(combina.RispostaIlleggibile):
        combina.leggi_probabilita("Penso che sia probabile.")


def test_binarie_mediana():
    assert combina.combina_binarie([0.1, 0.2, 0.9, 0.3, 0.25]) == pytest.approx(0.25)


def test_binarie_mediana_limitata():
    assert combina.combina_binarie([0.0, 0.001, 0.002]) == 0.01


def test_binarie_vuote():
    with pytest.raises(ValueError):
        combina.combina_binarie([])


# --------------------------------------------------- scelta multipla
OPZIONI = ["Anthropic", "Google", "OpenAI", "Other"]


def test_legge_opzioni_percentuali():
    testo = "ragionamento...\nAnthropic: 40%\nGoogle: 30%\nOpenAI: 20%\nOther: 10%"
    p = combina.leggi_opzioni(testo, OPZIONI)
    assert p["Anthropic"] == pytest.approx(0.4, abs=0.01)
    assert sum(p.values()) == pytest.approx(1.0)


def test_legge_opzioni_decimali_e_prefisso():
    testo = "- Option Anthropic: 0.5\n- Option Google: 0.25\n- Option OpenAI: 0.2\n- Option Other: 0.05"
    p = combina.leggi_opzioni(testo, OPZIONI)
    assert p["Google"] == pytest.approx(0.25, abs=0.01)


def test_opzione_mancante():
    with pytest.raises(combina.RispostaIlleggibile):
        combina.leggi_opzioni("Anthropic: 50%\nGoogle: 50%", OPZIONI)


def test_opzioni_pavimento_e_somma_uno():
    p = combina.normalizza_opzioni({"A": 1.0, "B": 0.0, "C": 0.0})
    assert min(p.values()) > 0
    assert sum(p.values()) == pytest.approx(1.0)


def test_opzioni_media():
    a = {"A": 0.6, "B": 0.4}
    b = {"A": 0.2, "B": 0.8}
    p = combina.combina_opzioni([a, b])
    assert p["A"] == pytest.approx(0.4)
    assert p["B"] == pytest.approx(0.6)


def test_opzioni_diverse_rifiutate():
    with pytest.raises(ValueError):
        combina.combina_opzioni([{"A": 1.0}, {"B": 1.0}])


# ------------------------------------------- distribuzione numerica
RISPOSTA = """
Percentile 10: 50
Percentile 20: 60
Percentile 40: 70
Percentile 60: 80
Percentile 80: 95
Percentile 90: 1,200
"""


def test_legge_percentili_con_migliaia():
    punti = combina.leggi_percentili(RISPOSTA)
    assert [p for p, _ in punti] == [0.1, 0.2, 0.4, 0.6, 0.8, 0.9]
    assert punti[-1][1] == 1200


def test_percentile_mancante():
    with pytest.raises(combina.RispostaIlleggibile):
        combina.leggi_percentili("Percentile 10: 5\nPercentile 90: 9")


def test_legge_date():
    testo = "\n".join(f"Percentile {p}: 2027-0{i + 1}-15" for i, p in enumerate((10, 20, 40, 60, 80, 90)))
    punti = combina.leggi_percentili(testo, data=True)
    assert punti[0][1] == datetime(2027, 1, 15, tzinfo=timezone.utc).timestamp()


def _domanda(aperto_basso=False, aperto_alto=False, basso=0.0, alto=100.0):
    return NumericQuestion(
        question_text="prova", lower_bound=basso, upper_bound=alto,
        open_lower_bound=aperto_basso, open_upper_bound=aperto_alto,
    )


@pytest.mark.parametrize(
    "valori",
    [
        [10, 20, 30, 40, 50, 60],          # normale
        [60, 50, 40, 30, 20, 10],          # al contrario
        [30, 30, 30, 30, 30, 30],          # tutti uguali
        [-50, -10, 5, 50, 150, 400],       # fuori dai limiti
        [99.99, 100, 100, 100, 100, 100],  # schiacciati sul limite alto
        [0, 0, 0, 0, 0.0001, 0.0002],      # schiacciati sul limite basso
    ],
)
@pytest.mark.parametrize("aperto", [False, True])
def test_distribuzione_pulita_e_monotona(valori, aperto):
    domanda = _domanda(aperto_basso=aperto, aperto_alto=aperto)
    punti = list(zip([0.1, 0.2, 0.4, 0.6, 0.8, 0.9], valori))
    puliti = combina.pulisci_percentili(punti, 0.0, 100.0, aperto, aperto)
    valori_puliti = [v for _, v in puliti]
    assert all(b > a for a, b in zip(valori_puliti, valori_puliti[1:]))
    if not aperto:
        assert 0.0 < valori_puliti[0] and valori_puliti[-1] < 100.0
    distribuzione = NumericDistribution.from_question(
        [Percentile(percentile=p, value=v) for p, v in puliti], domanda
    )
    cdf = [p.percentile for p in distribuzione.get_cdf()]
    assert len(cdf) == domanda.cdf_size
    assert combina.cdf_monotona(cdf)
    assert 0.0 <= cdf[0] and cdf[-1] <= 1.0


def test_distribuzione_data():
    adesso = datetime(2026, 10, 14, tzinfo=timezone.utc)
    domanda = DateQuestion(
        question_text="prova", lower_bound=adesso, upper_bound=adesso + timedelta(days=365),
        open_lower_bound=False, open_upper_bound=True,
    )
    testo = "\n".join(f"Percentile {p}: 2027-03-01" for p in (10, 20, 40, 60, 80, 90))
    punti = combina.leggi_percentili(testo, data=True)
    puliti = combina.pulisci_percentili(
        punti, domanda.lower_bound.timestamp(), domanda.upper_bound.timestamp(), False, True
    )
    distribuzione = NumericDistribution.from_question(
        [Percentile(percentile=p, value=v) for p, v in puliti], domanda
    )
    assert combina.cdf_monotona([p.percentile for p in distribuzione.get_cdf()])


def test_limiti_non_validi():
    with pytest.raises(ValueError):
        combina.pulisci_percentili([(0.1, 1), (0.9, 2)], 10, 10, False, False)
