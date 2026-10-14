"""
Modelli linguistici con tetti di spesa e registro.

LlmRegistrato estende GeneralLlm di forecasting-tools: prima di ogni chiamata
controlla i tetti, dopo la chiamata scrive modello, token e costo nel registro.
LlmFinto risponde senza rete, per le prove quando nel .env non ci sono chiavi:
i token sono stimati dalla lunghezza dei testi, così il costo per domanda
calcolato con i prezzi veri resta una stima realistica.
"""
from __future__ import annotations

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
            "(a) Frequenza di base: eventi simili accadono raramente.\n"
            "(b) Tempo rimasto: alcune settimane.\n"
            "(c) Se non cambia niente: vale lo status quo.\n"
        )
        # Le risposte ai prompt di previsione simulano anche i token di ragionamento.
        if "Answer format: probability" in testo:
            p = self._caso.uniform(0.15, 0.45)
            return ragionamento + f"Probability: {p * 100:.0f}%", 1800
        if "Answer format: option probabilities" in testo:
            opzioni = re.findall(r"^\s*- Option: (.+)$", testo, flags=re.MULTILINE)
            pesi = [self._caso.uniform(1, 5) for _ in opzioni]
            totale = sum(pesi)
            righe = [f"{o}: {round(100 * w / totale)}%" for o, w in zip(opzioni, pesi)]
            return ragionamento + "\n".join(righe), 1800
        if "Answer format: percentiles" in testo:
            data = "Question type: date" in testo
            basso = _valore(re.search(r"^\s*Lower bound: (\S+)", testo, flags=re.MULTILINE).group(1), data)
            alto = _valore(re.search(r"^\s*Upper bound: (\S+)", testo, flags=re.MULTILINE).group(1), data)
            centro = self._caso.uniform(0.35, 0.65)
            punti = {10: -0.25, 20: -0.15, 40: -0.04, 60: 0.04, 80: 0.15, 90: 0.25}
            righe = []
            for percentile, scarto in punti.items():
                valore = basso + (alto - basso) * min(0.98, max(0.02, centro + scarto))
                if data:
                    testo_valore = datetime.fromtimestamp(valore, tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    testo_valore = f"{valore:.4f}"
                righe.append(f"Percentile {percentile}: {testo_valore}")
            return ragionamento + "\n".join(righe), 1800
        if "DOMANDE_MANCANTI" in testo:
            return "- ultime notizie sul tema\n- dati storici di confronto", 120
        if "RIASSUNTO_RICERCA" in testo:
            return "Riassunto finto della ricerca (2026-10-14): nessuna novità rilevante.", 300
        return "Note di ricerca finte (fonte: prova offline, 2026-10-14).", 600


def _valore(testo: str, data: bool) -> float:
    if data:
        return datetime.strptime(testo[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return float(testo)


def crea_llm(modello: str, registro: Registro, scopo: str, finto: bool, **kwargs) -> LlmRegistrato:
    if finto or modello.startswith("finto/"):
        return LlmFinto(model=modello, registro=registro, scopo=scopo, **kwargs)
    return LlmRegistrato(model=modello, registro=registro, scopo=scopo, **kwargs)
