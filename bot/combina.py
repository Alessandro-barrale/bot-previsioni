"""
Lettura delle risposte dei modelli e combinazione delle previsioni.

- sì/no: mediana, poi limitata tra 1% e 99%;
- scelta multipla: media per opzione, poi pavimento minimo e rinormalizzazione;
- numeriche e date: percentili ordinati e resi strettamente crescenti dentro
  i limiti consentiti; la combinazione è la mediana delle CDF (forecasting-tools).

Tutto qui è puro (niente rete), così i test lo coprono per intero.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone

PROB_MIN = 0.01
PROB_MAX = 0.99
PAVIMENTO_OPZIONE = 0.005
PERCENTILI_ATTESI = (10, 20, 40, 60, 80, 90)


class RispostaIlleggibile(ValueError):
    pass


# --------------------------------------------------------------- sì/no
def limita_probabilita(p: float) -> float:
    return max(PROB_MIN, min(PROB_MAX, float(p)))


def leggi_probabilita(testo: str) -> float:
    """Ultima riga 'Probability: NN%' del testo, in decimale."""
    trovate = re.findall(r"Probability\s*:\s*\**\s*([0-9]+(?:[.,][0-9]+)?)\s*%", testo, flags=re.IGNORECASE)
    if not trovate:
        raise RispostaIlleggibile("nessuna riga 'Probability: NN%'")
    valore = float(trovate[-1].replace(",", "."))
    if not 0 <= valore <= 100:
        raise RispostaIlleggibile(f"probabilità fuori scala: {valore}")
    return limita_probabilita(valore / 100)


def combina_binarie(previsioni: list[float]) -> float:
    if not previsioni:
        raise ValueError("nessuna previsione da combinare")
    return limita_probabilita(statistics.median(previsioni))


# ---------------------------------------------------- scelta multipla
def _normalizza_nome(nome: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", nome.lower()).strip()


def leggi_opzioni(testo: str, opzioni: list[str]) -> dict[str, float]:
    """Cerca 'Opzione: NN%' per ogni opzione; prende l'ultima occorrenza."""
    risultato: dict[str, float] = {}
    righe = testo.splitlines()
    for opzione in opzioni:
        chiave = _normalizza_nome(opzione)
        trovato = None
        for riga in righe:
            m = re.match(r"^\s*[-*]?\s*(?:Option\s*[:\-]?\s*)?(.+?)\s*[:=]\s*\**\s*([0-9]+(?:[.,][0-9]+)?)\s*%?\s*\**\s*$", riga, flags=re.IGNORECASE)
            if m and _normalizza_nome(m.group(1)).removeprefix("option ") == chiave:
                trovato = float(m.group(2).replace(",", "."))
        if trovato is None:
            raise RispostaIlleggibile(f"manca la probabilità per l'opzione {opzione!r}")
        risultato[opzione] = trovato
    totale = sum(risultato.values())
    if totale <= 0:
        raise RispostaIlleggibile("probabilità delle opzioni tutte a zero")
    # Le risposte possono essere in percentuale o in decimali.
    if totale > 1.5:
        risultato = {k: v / 100 for k, v in risultato.items()}
    return normalizza_opzioni(risultato)


def normalizza_opzioni(probabilita: dict[str, float]) -> dict[str, float]:
    """Pavimento minimo per ogni opzione, poi somma 1."""
    con_pavimento = {k: max(PAVIMENTO_OPZIONE, float(v)) for k, v in probabilita.items()}
    totale = sum(con_pavimento.values())
    return {k: v / totale for k, v in con_pavimento.items()}


def combina_opzioni(previsioni: list[dict[str, float]]) -> dict[str, float]:
    if not previsioni:
        raise ValueError("nessuna previsione da combinare")
    nomi = list(previsioni[0].keys())
    for p in previsioni:
        if set(p.keys()) != set(nomi):
            raise ValueError("le previsioni hanno opzioni diverse")
    media = {n: sum(p[n] for p in previsioni) / len(previsioni) for n in nomi}
    return normalizza_opzioni(media)


# ------------------------------------------------- numeriche e date
def _in_numero(valore: str, data: bool) -> float:
    valore = valore.strip().strip("*").strip()
    if data:
        testo = valore[:19].replace("Z", "")
        for formato in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(testo[: len(datetime.now().strftime(formato))], formato).replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
        raise RispostaIlleggibile(f"data illeggibile: {valore!r}")
    pulito = valore.replace(",", "").replace("$", "").replace("%", "").strip()
    pulito = pulito.split()[0] if pulito else pulito
    try:
        return float(pulito)
    except ValueError as e:
        raise RispostaIlleggibile(f"numero illeggibile: {valore!r}") from e


def leggi_percentili(testo: str, data: bool = False) -> list[tuple[float, float]]:
    """Righe 'Percentile NN: valore' (ultima occorrenza per ogni percentile)."""
    trovati: dict[int, float] = {}
    for m in re.finditer(r"Percentile\s*([0-9]{1,2})\s*:\s*([^\n(]+)", testo, flags=re.IGNORECASE):
        percentile = int(m.group(1))
        if percentile in PERCENTILI_ATTESI:
            trovati[percentile] = _in_numero(m.group(2), data)
    mancanti = [p for p in PERCENTILI_ATTESI if p not in trovati]
    if mancanti:
        raise RispostaIlleggibile(f"mancano i percentili {mancanti}")
    return [(p / 100, trovati[p]) for p in PERCENTILI_ATTESI]


def pulisci_percentili(
    punti: list[tuple[float, float]],
    limite_basso: float,
    limite_alto: float,
    aperto_basso: bool,
    aperto_alto: bool,
) -> list[tuple[float, float]]:
    """
    Restituisce percentili con valori strettamente crescenti e dentro i limiti.

    - i valori vengono ordinati (un modello che li scrive al contrario non
      rompe la previsione: la CDF deve salire);
    - con un limite chiuso il valore viene tenuto appena dentro il limite;
    - con un limite aperto si lascia spazio fuori, ma non oltre metà intervallo;
    - valori uguali vengono distanziati di poco.
    """
    if len(punti) < 2:
        raise ValueError("servono almeno due percentili")
    ampiezza = limite_alto - limite_basso
    if ampiezza <= 0:
        raise ValueError("limiti della domanda non validi")
    margine = ampiezza * 1e-3
    minimo = limite_basso - (0.5 * ampiezza if aperto_basso else -margine)
    massimo = limite_alto + (0.5 * ampiezza if aperto_alto else -margine)

    percentili = sorted(p for p, _ in punti)
    valori = sorted(min(massimo, max(minimo, v)) for _, v in punti)
    passo = ampiezza * 1e-4
    for i in range(1, len(valori)):
        if valori[i] <= valori[i - 1]:
            valori[i] = valori[i - 1] + passo
    if valori[-1] > massimo:
        # Se l'ultimo è stato spinto fuori, ricompatta dall'alto verso il basso.
        valori[-1] = massimo
        for i in range(len(valori) - 2, -1, -1):
            if valori[i] >= valori[i + 1]:
                valori[i] = valori[i + 1] - passo
    return list(zip(percentili, valori))


def cdf_monotona(cdf: list[float]) -> bool:
    return all(b > a for a, b in zip(cdf, cdf[1:]))
