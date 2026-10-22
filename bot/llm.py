"""
Modelli linguistici con tetti di spesa e registro.

LlmRegistrato estende GeneralLlm di forecasting-tools: prima di ogni chiamata
controlla i tetti, dopo la chiamata scrive modello, token e costo nel registro.
LlmFinto risponde senza rete, per le prove quando nel .env non ci sono chiavi:
i token sono stimati dalla lunghezza dei testi, così il costo per domanda
calcolato con i prezzi veri resta una stima realistica.
"""
from __future__ import annotations

import os
import random
import re
from datetime import datetime, timezone

from forecasting_tools import GeneralLlm
from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse

from bot.spese import Registro, TettoSuperato, domanda_corrente

MAX_TOKEN_RISPOSTA = 16000


def _testo_prompt(prompt: object) -> str:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parti = []
        for messaggio in prompt:
            contenuto = messaggio.get("content", "") if isinstance(messaggio, dict) else str(messaggio)
            parti.append(contenuto if isinstance(contenuto, str) else str(contenuto))
        return "\n".join(parti)
    return str(prompt)


class LlmRegistrato(GeneralLlm):
    def __init__(self, model: str, registro: Registro, scopo: str, **kwargs) -> None:
        # I modelli Claude recenti rifiutano temperature: non la mandiamo.
        kwargs.setdefault("temperature", None)
        kwargs.setdefault("max_tokens", MAX_TOKEN_RISPOSTA)
        kwargs.setdefault("timeout", 300)
        kwargs.setdefault("allowed_tries", 2)
        super().__init__(model=model, **kwargs)
        self._registro = registro
        self._scopo = scopo

    async def _mockable_direct_call_to_model(self, prompt) -> TextTokenCostResponse:
        domanda = domanda_corrente.get()
        self._registro.controlla_tetti(domanda)
        try:
            risposta = await self._chiama(prompt)
        except TettoSuperato:
            raise
        except Exception:
            self._registro.registra_chiamata(
                self.model, 0, 0, costo=0.0, scopo=self._scopo, esito="errore"
            )
            raise
        costo = self._registro.registra_chiamata(
            self.model,
            risposta.prompt_tokens_used,
            risposta.completion_tokens_used,
            costo=risposta.cost or None,  # 0 = il fornitore non ha dato il costo
            scopo=self._scopo,
        )
        return risposta.model_copy(update={"cost": costo})

    async def _chiama(self, prompt) -> TextTokenCostResponse:
        return await super()._mockable_direct_call_to_model(prompt)


class LlmFinto(LlmRegistrato):
    """Modello finto: nessuna rete, risposte nel formato che il bot si aspetta."""

    def __init__(self, model: str, registro: Registro, scopo: str, seme: int | None = None, **kwargs) -> None:
        super().__init__(model=model, registro=registro, scopo=scopo, **kwargs)
        self._caso = random.Random(seme)

    async def _chiama(self, prompt) -> TextTokenCostResponse:
        testo = _testo_prompt(prompt)
        risposta, token_uscita = self._risposta(testo)
        token_ingresso = max(1, len(testo) // 4)
        return TextTokenCostResponse(
            data=risposta,
            prompt_tokens_used=token_ingresso,
            completion_tokens_used=token_uscita,
            total_tokens_used=token_ingresso + token_uscita,
            model=self.model,
            cost=0.0,  # il registro lo ricalcola con i prezzi del modello imitato
        )

    def _risposta(self, testo: str) -> tuple[str, int]:
        ragionamento = (
            "1. Status quo: no qualifying event has yet occurred inside the window.\n"
            "2. Resolution check: not yet satisfied.\n"
            "3. Base rate: events like this happen rarely; anchored on the calculation.\n"
            "7. Calibration check done.\n"
        )
        # Le risposte ai prompt di previsione simulano anche i token di ragionamento
        # e chiudono con il blocco JSON obbligatorio della v2.
        if "Answer format: probability" in testo:
            p = self._caso.uniform(0.15, 0.45)
            return ragionamento + f"Probability: {p * 100:.0f}%\n```json\n{{\"probability\": {p:.3f}}}\n```", 1800
        if "Answer format: option probabilities" in testo:
            opzioni = re.findall(r"^\s*- Option: (.+)$", testo, flags=re.MULTILINE)
            pesi = [self._caso.uniform(1, 5) for _ in opzioni]
            totale = sum(pesi)
            corpo = ", ".join(f'"{o}": {w / totale:.3f}' for o, w in zip(opzioni, pesi))
            return ragionamento + '```json\n{"option_probabilities": {' + corpo + "}}\n```", 1800
        if "Answer format: percentiles" in testo:
            data = "Question type: date" in testo
            basso = _valore(re.search(r"^\s*Lower bound: (\S+)", testo, flags=re.MULTILINE).group(1), data)
            alto = _valore(re.search(r"^\s*Upper bound: (\S+)", testo, flags=re.MULTILINE).group(1), data)
            centro = self._caso.uniform(0.35, 0.65)
            z = {1: -2.33, 2.5: -1.96, 5: -1.64, 10: -1.28, 20: -0.84, 40: -0.25, 50: 0.0, 60: 0.25,
                 80: 0.84, 90: 1.28, 95: 1.64, 97.5: 1.96, 99: 2.33}
            voci = []
            for percentile, scarto in z.items():
                valore = basso + (alto - basso) * min(0.98, max(0.02, centro + 0.12 * scarto))
                if data:
                    giorno = datetime.fromtimestamp(valore, tz=timezone.utc).strftime("%Y-%m-%d")
                    voci.append(f'"{percentile:g}": "{giorno}"')
                else:
                    voci.append(f'"{percentile:g}": {valore:.4f}')
            return ragionamento + '```json\n{"declared_percentiles": {' + ", ".join(voci) + "}}\n```", 1800
        if "QUERY_MERCATI" in testo:
            return '```json\n{"queries": ["European Central Bank", "ECB rate cut"]}\n```', 30
        if "ABBINAMENTO_MERCATI" in testo:
            return (
                '```json\n{"matches": [{"n": 1, "relation": "related", "why": "same topic, different date"}]}\n```',
                80,
            )
        if "SERIE_STORICHE" in testo:
            return (
                '```json\n{"fred_series_id": null, "yahoo_ticker": "BZ=F", "note": "Brent front-month future"}\n```',
                60,
            )
        if "TRIMESTRALI_AZIENDA" in testo:
            return '```json\n{"company": "Example Corp", "ticker": "EXMP"}\n```', 30
        if "DOMANDE_MANCANTI" in testo:
            return "- ultime notizie sul tema\n- dati storici di confronto", 120
        if "RIASSUNTO_RICERCA" in testo:
            return "Riassunto finto della ricerca (2026-10-14): nessuna novità rilevante.", 300
        return "Note di ricerca finte (fonte: prova offline, 2026-10-14).", 600


def _valore(testo: str, data: bool) -> float:
    if data:
        return datetime.strptime(testo[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return float(testo)


def parametri_modello(modello: str, scopo: str) -> dict:
    """
    Parametri extra per fornitore. Previsioni con effort alto (EFFORT_PREVISIONE,
    predefinito "high") sui modelli OpenAI e Google via OpenRouter; sui modelli
    Anthropic solo se EFFORT_ANTHROPIC è impostato (oggi girano senza, e il
    costo del ragionamento esteso non è ancora misurato). Mai `verbosity`
    insieme a `reasoning_effort` su Anthropic via OpenRouter: vincerebbe
    verbosity e l'effort dichiarato non si applicherebbe.
    """
    if scopo != "previsione":
        return {}
    anthropic = "claude" in modello or "anthropic" in modello
    effort = (os.getenv("EFFORT_ANTHROPIC") if anthropic else os.getenv("EFFORT_PREVISIONE", "high")) or ""
    effort = effort.strip().lower()
    if effort in {"", "no", "none", "off"}:
        return {}
    return {"reasoning_effort": effort}


def crea_llm(modello: str, registro: Registro, scopo: str, finto: bool, **kwargs) -> LlmRegistrato:
    if finto or modello.startswith("finto/"):
        return LlmFinto(model=modello, registro=registro, scopo=scopo, **kwargs)
    for chiave, valore in parametri_modello(modello, scopo).items():
        kwargs.setdefault(chiave, valore)
    kwargs.pop("verbosity", None)
    return LlmRegistrato(model=modello, registro=registro, scopo=scopo, **kwargs)
