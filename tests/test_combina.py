from datetime import datetime, timedelta, timezone

import pytest
from forecasting_tools import DateQuestion, NumericDistribution, NumericQuestion, Percentile
from forecasting_tools.data_models.numeric_report import NumericReport

from bot import combina


# ------------------------------------------------------------ JSON finale
def test_json_finale_ultimo_blocco():
    testo = "bozza\n```json\n{\"probability\": 0.8}\n```\nripensamento\n```json\n{\"probability\": 0.3}\n```"
    assert combina.leggi_json_finale(testo) == {"probability": 0.3}


def test_json_finale_nudo_in_coda():
    assert combina.leggi_json_finale("testo {\"a\": 1} altro {\"probability\": 0.25}") == {"probability": 0.25}


def test_json_finale_assente_o_rotto():
    assert combina.leggi_json_finale("niente json") is None
    assert combina.leggi_json_finale("```json\n{\"probability\": }\n```") is None


# ------------------------------------------------------------ sì/no
def test_legge_probabilita_dal_json():
    testo = "Probability: 80%\n...\n```json\n{\"probability\": 0.37}\n```"
    assert combina.leggi_probabilita(testo) == pytest.approx(0.37)


def test_legge_probabilita_json_in_percento():
    assert combina.leggi_probabilita("{\"probability\": 37}") == pytest.approx(0.37)
    assert combina.leggi_probabilita("{\"probability\": \"37%\"}") == pytest.approx(0.37)


def test_riserva_regex_ultima_probabilita():
    testo = "Prima pensavo Probability: 80%\n...\nProbability: 35%"
    assert combina.leggi_probabilita(testo) == pytest.approx(0.35)


def test_probabilita_con_decimali_e_grassetto():
    assert combina.leggi_probabilita("**Probability: 12.5%**") == pytest.approx(0.125)


def test_probabilita_limitata_tra_2_e_98():
    assert combina.leggi_probabilita("Probability: 0%") == 0.02
    assert combina.leggi_probabilita("Probability: 100%") == 0.98
    assert combina.leggi_probabilita("{\"probability\": 0.999}") == 0.98


def test_probabilita_mancante():
    with pytest.raises(combina.RispostaIlleggibile):
        combina.leggi_probabilita("Penso che sia probabile.")


def test_binarie_mediana():
    assert combina.combina_binarie([0.1, 0.2, 0.9, 0.3, 0.25]) == pytest.approx(0.25)


def test_binarie_mediana_limitata():
    assert combina.combina_binarie([0.0, 0.001, 0.002]) == 0.02


def test_binarie_vuote():
    with pytest.raises(ValueError):
        combina.combina_binarie([])


# --------------------------------------------------- scelta multipla
OPZIONI = ["Anthropic", "Google", "OpenAI", "Other"]


def test_legge_opzioni_dal_json():
    testo = (
        "ragionamento Anthropic: 90%\n```json\n"
        "{\"option_probabilities\": {\"Anthropic\": 0.4, \"Google\": 0.3, \"OpenAI\": 0.2, \"Other\": 0.1}}\n```"
    )
    p = combina.leggi_opzioni(testo, OPZIONI)
    assert p["Anthropic"] == pytest.approx(0.4, abs=0.01)
    assert sum(p.values()) == pytest.approx(1.0)


def test_legge_opzioni_json_come_lista():
    testo = "{\"options\": [" + ", ".join(f"{{\"option\": \"{o}\", \"probability\": 25}}" for o in OPZIONI) + "]}"
    p = combina.leggi_opzioni(testo, OPZIONI)
    assert p["Google"] == pytest.approx(0.25)


def test_riserva_regex_opzioni_percentuali():
    testo = "ragionamento...\nAnthropic: 40%\nGoogle: 30%\nOpenAI: 20%\nOther: 10%"
    p = combina.leggi_opzioni(testo, OPZIONI)
    assert p["Anthropic"] == pytest.approx(0.4, abs=0.01)


def test_legge_opzioni_decimali_e_prefisso():
    testo = "- Option Anthropic: 0.5\n- Option Google: 0.25\n- Option OpenAI: 0.2\n- Option Other: 0.05"
    p = combina.leggi_opzioni(testo, OPZIONI)
    assert p["Google"] == pytest.approx(0.25, abs=0.01)


def test_opzione_mancante_anche_nel_json():
    with pytest.raises(combina.RispostaIlleggibile):
        combina.leggi_opzioni("{\"option_probabilities\": {\"Anthropic\": 0.5, \"Google\": 0.5}}", OPZIONI)


def test_opzioni_pavimento_uno_per_cento_e_somma_uno():
    p = combina.normalizza_opzioni({"A": 1.0, "B": 0.0, "C": 0.0})
    assert min(p.values()) == pytest.approx(0.01 / 1.02)
    assert sum(p.values()) == pytest.approx(1.0)


def test_opzioni_media_con_cinque_campioni():
    campioni = [{"A": 0.6, "B": 0.4}] * 4 + [{"A": 0.1, "B": 0.9}]
    p = combina.combina_opzioni(campioni)
    assert p["A"] == pytest.approx(0.5)


def test_opzioni_mediana_con_tre_campioni():
    campioni = [{"A": 0.6, "B": 0.4}, {"A": 0.5, "B": 0.5}, {"A": 0.1, "B": 0.9}]
    p = combina.combina_opzioni(campioni)
    assert p["A"] == pytest.approx(0.5)


def test_opzioni_diverse_rifiutate():
    with pytest.raises(ValueError):
        combina.combina_opzioni([{"A": 1.0}, {"B": 1.0}])


# ------------------------------------------- distribuzione numerica
VALORI_13 = [40, 45, 50, 55, 62, 70, 75, 80, 90, 100, 110, 120, 1200]


def _json_percentili(valori, data=False):
    voci = []
    for p, v in zip(combina.PERCENTILI_V2, valori):
        voci.append(f"\"{p:g}\": {repr(v) if data else v}".replace("'", '"'))
    return "```json\n{\"declared_percentiles\": {" + ", ".join(voci) + "}}\n```"


def test_legge_13_percentili_dal_json():
    punti = combina.leggi_percentili("ragionamento\n" + _json_percentili(VALORI_13))
    assert [round(p * 100, 1) for p, _ in punti] == [1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99]
    assert punti[-1][1] == 1200


def test_legge_percentili_json_chiavi_decimali_e_stringhe():
    testo = "{\"percentiles\": {" + ", ".join(f"\"{p / 100}\": \"{v:,}\"" for p, v in zip(combina.PERCENTILI_V2, VALORI_13)) + "}}"
    punti = combina.leggi_percentili(testo)
    assert punti[-1][1] == 1200


def test_json_incompleto_cade_sulla_regex():
    testo = "{\"declared_percentiles\": {\"10\": 1, \"90\": 2}}\n" + "\n".join(
        f"Percentile {p:g}: {v}" for p, v in zip(combina.PERCENTILI_V2, VALORI_13)
    )
    punti = combina.leggi_percentili(testo)
    assert len(punti) == 13 and punti[0][1] == 40


def test_riserva_regex_sei_percentili_v1():
    testo = "Percentile 10: 50\nPercentile 20: 60\nPercentile 40: 70\nPercentile 60: 80\nPercentile 80: 95\nPercentile 90: 1,200"
    punti = combina.leggi_percentili(testo)
    assert [p for p, _ in punti] == [0.1, 0.2, 0.4, 0.6, 0.8, 0.9]
    assert punti[-1][1] == 1200


def test_percentile_mancante():
    with pytest.raises(combina.RispostaIlleggibile):
        combina.leggi_percentili("Percentile 10: 5\nPercentile 90: 9")


def test_legge_date_dal_json():
    date = [f"2027-{m:02d}-15" for m in range(1, 13)] + ["2028-01-15"]
    punti = combina.leggi_percentili(_json_percentili(date, data=True), data=True)
    assert punti[0][1] == datetime(2027, 1, 15, tzinfo=timezone.utc).timestamp()
    assert all(b > a for (_, a), (_, b) in zip(punti, punti[1:]))


def test_legge_date_regex():
    testo = "\n".join(f"Percentile {p}: 2027-0{i + 1}-15" for i, p in enumerate((10, 20, 40, 60, 80, 90)))
    punti = combina.leggi_percentili(testo, data=True)
    assert punti[0][1] == datetime(2027, 1, 15, tzinfo=timezone.utc).timestamp()


def _domanda(aperto_basso=False, aperto_alto=False, basso=0.0, alto=100.0):
    return NumericQuestion(
        question_text="prova", lower_bound=basso, upper_bound=alto,
        open_lower_bound=aperto_basso, open_upper_bound=aperto_alto,
    )


LIVELLI_13 = [p / 100 for p in combina.PERCENTILI_V2]


@pytest.mark.parametrize(
    "valori",
    [
        list(range(10, 75, 5)),                        # normale
        list(range(70, 5, -5)),                        # al contrario
        [30] * 13,                                     # tutti uguali
        [-80, -50, -10, 5, 20, 40, 50, 60, 80, 120, 150, 300, 400],  # fuori dai limiti
        [99.9] + [100] * 12,                           # schiacciati sul limite alto
        [0] * 11 + [0.0001, 0.0002],                   # schiacciati sul limite basso
    ],
)
@pytest.mark.parametrize("aperto", [False, True])
def test_distribuzione_pulita_e_monotona(valori, aperto):
    domanda = _domanda(aperto_basso=aperto, aperto_alto=aperto)
    punti = list(zip(LIVELLI_13, valori))
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
    testo = "\n".join(f"Percentile {p:g}: 2027-03-01" for p in combina.PERCENTILI_V2)
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


def _distribuzione(domanda, centro, larghezza):
    z = [-2.33, -1.96, -1.64, -1.28, -0.84, -0.25, 0.0, 0.25, 0.84, 1.28, 1.64, 1.96, 2.33]
    punti = combina.pulisci_percentili(
        [(p, centro + larghezza * s) for p, s in zip(LIVELLI_13, z)], 0.0, 100.0, False, False
    )
    return NumericDistribution.from_question([Percentile(percentile=p, value=v) for p, v in punti], domanda)


async def test_numeriche_mediana_delle_cdf_non_dei_percentili():
    """
    Tre campioni: due stretti e vicini, uno largo e lontano. La media dei
    percentili sposterebbe la distribuzione verso il terzo; la mediana punto
    per punto delle CDF (quella usata dal bot) no.
    """
    domanda = _domanda()
    campioni = [_distribuzione(domanda, 30, 3), _distribuzione(domanda, 32, 3), _distribuzione(domanda, 70, 15)]
    combinata = await NumericReport.aggregate_predictions(campioni, domanda)
    cdf_combinata = [p.percentile for p in combinata.get_cdf()]

    cdfs = [[p.percentile for p in c.get_cdf()] for c in campioni]
    attesa = combina.mediana_cdf(cdfs)
    # La libreria ri-standardizza la CDF combinata (passo minimo per bin): scarti sotto l'1%.
    assert cdf_combinata == pytest.approx(attesa, abs=0.01)

    # La media dei percentili darebbe una mediana intorno a 44: la mediana delle CDF resta vicino a 32.
    media_percentili = [
        (p, sum(c.declared_percentiles[i].value for c in campioni) / 3) for i, p in enumerate(LIVELLI_13)
    ]
    mediana_da_media = next(v for p, v in media_percentili if p == 0.5)
    assert mediana_da_media > 40
    ascisse = [p.value for p in combinata.get_cdf()]
    mediana_combinata = next(x for x, y in zip(ascisse, cdf_combinata) if y >= 0.5)
    assert 29 < mediana_combinata < 36
    assert combina.cdf_monotona(cdf_combinata)


def test_mediana_cdf_rifiuta_lunghezze_diverse():
    with pytest.raises(ValueError):
        combina.mediana_cdf([[0.1, 0.5], [0.1, 0.5, 0.9]])
