"""
Impostazioni del bot lette dalle variabili d'ambiente (file .env in locale,
segreti e variabili di GitHub in Actions). Nessuna chiave è scritta qui:
si guarda solo se una chiave c'è, mai il suo valore.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CARTELLA_PROGETTO = Path(__file__).resolve().parent.parent

_SEGNAPOSTO = {"", "REPLACE_ME", "1234567890", "your-token-here", "your-api-key-here"}


def chiave_presente(nome: str) -> bool:
    valore = os.getenv(nome)
    return valore is not None and valore.strip() not in _SEGNAPOSTO


def _float(nome: str, predefinito: float) -> float:
    valore = os.getenv(nome)
    if valore is None or not valore.strip():
        return predefinito
    return float(valore.replace(",", "."))


def _int(nome: str, predefinito: int) -> int:
    valore = os.getenv(nome)
    if valore is None or not valore.strip():
        return predefinito
    return int(valore)


def _lista(nome: str) -> list[str]:
    valore = os.getenv(nome, "")
    return [v.strip() for v in valore.split(",") if v.strip()]


def _si(nome: str) -> bool:
    return os.getenv(nome, "").strip().lower() in {"1", "si", "sì", "true", "yes", "on"}


@dataclass
class Impostazioni:
    # Invio vero delle previsioni a Metaculus: spento finché l'utente non lo accende.
    invia_previsioni: bool = False
    modello_finto: bool = False

    # Modelli. Le previsioni ruotano sulla lista, così con due modelli
    # le 5 previsioni si dividono 3 + 2.
    modelli_previsione: list[str] = field(default_factory=list)
    modello_ricerca: str = ""
    modello_riassunto: str = ""
    previsioni_per_domanda: int = 5

    # Ricerca
    giri_ricerca: int = 2
    ricerche_web_per_giro: int = 3

    # Tetti di spesa in dollari
    tetto_domanda: float = 0.80
    tetto_giorno: float = 15.0
    tetto_mese: float = 120.0
    obiettivo_medio_domanda: float = 0.40

    domande_in_parallelo: int = 3
    percorso_registro: Path = CARTELLA_PROGETTO / "dati" / "registro.sqlite"

    @classmethod
    def da_ambiente(cls) -> "Impostazioni":
        finto = _si("MODELLO_FINTO")
        modelli = _lista("MODELLI_PREVISIONE") or _modelli_predefiniti(finto)
        percorso = os.getenv("PERCORSO_REGISTRO")
        return cls(
            invia_previsioni=_si("INVIA_PREVISIONI"),
            modello_finto=finto,
            modelli_previsione=modelli,
            modello_ricerca=os.getenv("MODELLO_RICERCA") or _modello_ricerca_predefinito(finto),
            modello_riassunto=os.getenv("MODELLO_RIASSUNTO") or _modello_riassunto_predefinito(finto),
            previsioni_per_domanda=_int("PREVISIONI_PER_DOMANDA", 5),
            giri_ricerca=_int("GIRI_RICERCA", 2),
            ricerche_web_per_giro=_int("RICERCHE_WEB_PER_GIRO", 3),
            tetto_domanda=_float("TETTO_DOMANDA_USD", 0.80),
            tetto_giorno=_float("TETTO_GIORNO_USD", 15.0),
            tetto_mese=_float("TETTO_MESE_USD", 120.0),
            obiettivo_medio_domanda=_float("OBIETTIVO_MEDIO_DOMANDA_USD", 0.40),
            domande_in_parallelo=_int("DOMANDE_IN_PARALLELO", 3),
            percorso_registro=Path(percorso) if percorso else CARTELLA_PROGETTO / "dati" / "registro.sqlite",
        )


# Nomi dei modelli nel formato di litellm (prefisso = fornitore).
# - chiave Anthropic diretta (crediti API):  anthropic/claude-...
# - OpenRouter (crediti gratuiti Metaculus): openrouter/anthropic/claude-...
# - proxy Metaculus (solo METACULUS_TOKEN):  metaculus/claude-...


def _nome_claude(modello: str) -> str:
    """modello è il nome Anthropic, per esempio 'claude-opus-5-5'."""
    if chiave_presente("ANTHROPIC_API_KEY"):
        return f"anthropic/{modello}"
    if chiave_presente("OPENROUTER_API_KEY"):
        # OpenRouter scrive la versione con il punto: claude-opus-5.5
        testa, _, versione = modello.rpartition("-")
        testa2, _, versione_maggiore = testa.rpartition("-")
        return f"openrouter/anthropic/{testa2}-{versione_maggiore}.{versione}"
    return f"metaculus/{modello}"


def _modelli_predefiniti(finto: bool) -> list[str]:
    if finto:
        return ["finto/claude-opus-5-5", "finto/claude-sonnet-5-5"]
    # Due modelli diversi: con 5 previsioni la rotazione fa 3 Opus + 2 Sonnet.
    # Un terzo fornitore (per esempio un modello OpenAI via OpenRouter) si
    # aggiunge con MODELLI_PREVISIONE, vedi README.
    return [_nome_claude("claude-opus-5-5"), _nome_claude("claude-sonnet-5-5")]


def _modello_ricerca_predefinito(finto: bool) -> str:
    if finto:
        return "finto/claude-sonnet-5-5"
    return _nome_claude("claude-sonnet-5-5")


def _modello_riassunto_predefinito(finto: bool) -> str:
    if finto:
        return "finto/claude-haiku-5-5"
    return _nome_claude("claude-haiku-5-5")
