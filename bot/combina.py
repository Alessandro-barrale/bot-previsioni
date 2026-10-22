"""
Lettura delle risposte dei modelli e combinazione delle previsioni.

Lettura (v2): ogni risposta chiude con un blocco JSON obbligatorio; se il
blocco manca o è rotto si usano le espressioni regolari della v1 come riserva.

Combinazione:
- sì/no: mediana, poi limitata tra 2% e 98%;
- scelta multipla: media per opzione, pavimento 1% e rinormalizzazione;
- numeriche e date: 13 percentili (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95,
  97.5, 99) ordinati e resi strettamente crescenti dentro i limiti; la
  combinazione tra campioni è la mediana punto per punto delle CDF
  (forecasting-tools, verificata da un test), MAI la media dei percentili.

Tutto qui è puro (niente rete), così i test lo coprono per intero.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone

# Limiti misurati dai bot vincenti: floor più alti sono una tassa sul punteggio.
PROB_MIN = 0.02
PROB_MAX = 0.98
PAVIMENTO_OPZIONE = 0.01
PERCENTILI_V2 = (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99)
PERCENTILI_V1 = (10, 20, 40, 60, 80, 90)
PERCENTILI_ATTESI = PERCENTILI_V2


class RispostaIlleggibile(ValueError):
    pass


# ------------------------------------------------------------- JSON finale
def leggi_json_finale(testo: str) -> dict | None:
    """
    L'ultimo oggetto JSON della risposta: prima si cercano i blocchi ```json,
    poi un oggetto {…} nudo in coda. None se non c'è niente di leggibile.
    """
    if not testo:
        return None
    blocchi = re.findall(r"```(?:json|JSON)?\s*\n?(.*?)```", testo, flags=re.DOTALL)
    for blocco in reversed(blocchi):
        oggetto = _parsa_oggetto(blocco)
        if oggetto is not None:
            return oggetto
    # Oggetti nudi: si scorre in avanti saltando l'interno di ogni oggetto
    # valido, così si prende l'ULTIMO oggetto di primo livello (non uno annidato).
    coda = testo[-6000:]
    decoder = json.JSONDecoder()
    ultimo: dict | None = None
    posizione = 0
    while True:
        inizio = coda.find("{", posizione)
        if inizio < 0:
            break
        try:
            oggetto, fine = decoder.raw_decode(coda[inizio:])
        except ValueError:
            posizione = inizio + 1
            continue
        if isinstance(oggetto, dict) and oggetto:
            ultimo = oggetto
        posizione = inizio + max(1, fine)
    return ultimo


def _parsa_oggetto(frammento: str) -> dict | None:
    frammento = frammento.strip()
    if not frammento.startswith("{"):
        return None
    try:
        oggetto = json.loads(frammento)
    except ValueError:
        return None
    return oggetto if isinstance(oggetto, dict) else None


def _cerca_chiave(oggetto: dict, *nomi: str):
    for nome in nomi:
        for chiave, valore in oggetto.items():
            if _normalizza_nome(str(chiave)) == _normalizza_nome(nome):
                return valore
    return None


# --------------------------------------------------------------- sì/no
def limita_probabilita(p: float) -> float:
    return max(PROB_MIN, min(PROB_MAX, float(p)))


def _in_probabilita(valore: object) -> float:
    if isinstance(valore, str):
        valore = valore.strip().rstrip("%").replace(",", ".")
    p = float(valore)  # ValueError se illeggibile
    if p > 1.0:
        p = p / 100.0
    if not 0 <= p <= 1:
        raise ValueError(f"probabilità fuori scala: {valore}")
    return p


def leggi_probabilita(testo: str) -> float:
    """Blocco JSON finale {"probability": 0.37}; riserva: ultima riga 'Probability: NN%'."""
    oggetto = leggi_json_finale(testo)
    if oggetto is not None:
        grezzo = _cerca_chiave(oggetto, "probability", "probability_yes", "final_probability")
        if grezzo is not None:
            try:
                return limita_probabilita(_in_probabilita(grezzo))
            except (TypeError, ValueError):
                pass
    trovate = re.findall(r"Probability\s*:\s*\**\s*([0-9]+(?:[.,][0-9]+)?)\s*%", testo, flags=re.IGNORECASE)
    if not trovate:
        raise RispostaIlleggibile("nessun blocco JSON con 'probability' né riga 'Probability: NN%'")
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


def _opzioni_da_json(oggetto: dict, opzioni: list[str]) -> dict[str, float] | None:
    grezzo = _cerca_chiave(oggetto, "option_probabilities", "probabilities", "options")
    if isinstance(grezzo, list):  # [{"option": "A", "probability": 0.4}, ...]
        try:
            grezzo = {
                str(_cerca_chiave(v, "option", "option_name", "name")): _cerca_chiave(v, "probability", "p")
                for v in grezzo
            }
        except (AttributeError, TypeError):
            return None
    if not isinstance(grezzo, dict):
        return None
    per_nome = {_normalizza_nome(str(k)): v for k, v in grezzo.items()}
    risultato: dict[str, float] = {}
    for opzione in opzioni:
        valore = per_nome.get(_normalizza_nome(opzione))
        if valore is None:
            return None
        try:
            risultato[opzione] = float(str(valore).strip().rstrip("%").replace(",", "."))
        except ValueError:
            return None
    return risultato


def leggi_opzioni(testo: str, opzioni: list[str]) -> dict[str, float]:
    """Blocco JSON finale {"option_probabilities": {...}}; riserva: righe 'Opzione: NN%'."""
    oggetto = leggi_json_finale(testo)
    risultato = _opzioni_da_json(oggetto, opzioni) if oggetto is not None else None
    if risultato is None:
        risultato = {}
        righe = testo.splitlines()
        for opzione in opzioni:
            chiave = _normalizza_nome(opzione)
            trovato = None
            for riga in righe:
                m = re.match(
                    r"^\s*[-*]?\s*(?:Option\s*[:\-]?\s*)?(.+?)\s*[:=]\s*\**\s*([0-9]+(?:[.,][0-9]+)?)\s*%?\s*\**\s*$",
                    riga,
                    flags=re.IGNORECASE,
                )
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
    """
    Media per opzione con 5 o più campioni; con meno campioni la mediana per
    opzione (poi rinormalizzata), come consigliano i bot vincenti.
    """
    if not previsioni:
        raise ValueError("nessuna previsione da combinare")
    nomi = list(previsioni[0].keys())
    for p in previsioni:
        if set(p.keys()) != set(nomi):
            raise ValueError("le previsioni hanno opzioni diverse")
    if len(previsioni) >= 5:
        combinata = {n: sum(p[n] for p in previsioni) / len(previsioni) for n in nomi}
    else:
        combinata = {n: statistics.median(p[n] for p in previsioni) for n in nomi}
    return normalizza_opzioni(combinata)


# ------------------------------------------------- numeriche e date
def _in_numero(valore: object, data: bool) -> float:
    if isinstance(valore, (int, float)) and not isinstance(valore, bool):
        if data:
            raise RispostaIlleggibile(f"data attesa, trovato un numero: {valore!r}")
        return float(valore)
    valore = str(valore).strip().strip("*").strip()
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


def _chiave_percentile(chiave: object) -> float | None:
    testo = str(chiave).strip().lower().lstrip("p").replace("percentile", "").replace("%", "").strip()
    try:
        return float(testo)
    except ValueError:
        return None


def _percentili_da_json(oggetto: dict, data: bool) -> dict[float, float] | None:
    grezzo = _cerca_chiave(oggetto, "declared_percentiles", "percentiles", "distribution")
    if isinstance(grezzo, list):
        try:
            grezzo = {_cerca_chiave(v, "percentile", "p"): _cerca_chiave(v, "value", "v") for v in grezzo}
        except (AttributeError, TypeError):
            return None
    if not isinstance(grezzo, dict):
        return None
    trovati: dict[float, float] = {}
    for chiave, valore in grezzo.items():
        p = _chiave_percentile(chiave)
        if p is None or valore is None:
            continue
        if 0 < p < 1:  # scritto in decimale (0.025) invece che in percento (2.5)
            p = p * 100
        try:
            trovati[round(p, 3)] = _in_numero(valore, data)
        except RispostaIlleggibile:
            return None
    return trovati or None


def _completa(trovati: dict[float, float]) -> list[tuple[float, float]] | None:
    for attesi in (PERCENTILI_V2, PERCENTILI_V1):
        if all(float(p) in trovati for p in attesi):
            return [(p / 100, trovati[float(p)]) for p in attesi]
    return None


def leggi_percentili(testo: str, data: bool = False) -> list[tuple[float, float]]:
    """
    Blocco JSON finale {"declared_percentiles": {"1": …, "2.5": …, …, "99": …}}
    con i 13 percentili; riserva: righe 'Percentile NN: valore' (13 o i 6 della v1).
    """
    oggetto = leggi_json_finale(testo)
    if oggetto is not None:
        trovati = _percentili_da_json(oggetto, data)
        if trovati:
            punti = _completa(trovati)
            if punti:
                return punti
    trovati = {}
    for m in re.finditer(r"Percentile\s*([0-9]{1,2}(?:\.[0-9]+)?)\s*:\s*([^\n(]+)", testo, flags=re.IGNORECASE):
        trovati[round(float(m.group(1)), 3)] = _in_numero(m.group(2), data)
    punti = _completa(trovati)
    if punti is None:
        mancanti = [p for p in PERCENTILI_V2 if float(p) not in trovati]
        raise RispostaIlleggibile(f"mancano i percentili {mancanti}")
    return punti


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


def mediana_cdf(cdfs: list[list[float]]) -> list[float]:
    """
    Mediana punto per punto di più CDF campionate sulla stessa ascissa.
    È la combinazione corretta per le numeriche: la media dei percentili
    (ascisse) tra campioni costa -7,4 log/domanda (misura nostreambot).
    """
    if not cdfs:
        raise ValueError("nessuna CDF da combinare")
    n = len(cdfs[0])
    if any(len(c) != n for c in cdfs):
        raise ValueError("le CDF hanno lunghezze diverse")
    return [statistics.median(c[i] for c in cdfs) for i in range(n)]
