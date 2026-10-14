"""
Il bot: estende FallTemplateBot2026 del template ufficiale.

Cosa cambia rispetto al template:
- ricerca a più fonti e a più giri (bot.ricerca);
- 5 previsioni per domanda, a rotazione su più modelli;
- prompt con il metodo da previsore: frequenza di base, tempo rimasto, status quo;
- lettura delle risposte con espressioni regolari (fallback: parser LLM);
- combinazione: mediana (sì/no), media (opzioni), CDF pulita e monotona (numeriche);
- tetti di spesa e registro SQLite di ogni chiamata e previsione;
- domande già previste (anche in prova) non vengono rifatte: non si spende due volte.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from forecasting_tools import (
    BinaryPrediction,
    BinaryQuestion,
    DateQuestion,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
    ReasonedPrediction,
    clean_indents,
    structure_output,
)
from forecasting_tools.data_models.forecast_report import ForecastReport

from bot import combina
from bot.config import Impostazioni
from bot.llm import LlmRegistrato
from bot.ricerca import Ricercatore
from bot.spese import Registro, TettoSuperato, domanda_corrente
from template_bot import FallTemplateBot2026

logger = logging.getLogger(__name__)

METODO = """
How good forecasters work, and how you must work:
1. Base rate: how often do events like this happen? Name the reference class and give a rough frequency.
2. Time left: how long until the question closes or resolves? Little time left means the status quo is very likely.
3. Status quo: what happens if nothing changes? The world usually changes slowly.
4. Evidence: what in the research moves you away from the base rate, and by how much? Check each fact's date.
5. Scenarios: briefly describe how each outcome could happen.
6. Calibration: avoid overconfidence, but do not hedge towards 50% without reason. Never go below 1% or above 99%.
Read the resolution criteria literally: the question resolves on what they say, not on the spirit of the title.
"""


def chiave_domanda(domanda: MetaculusQuestion) -> str:
    return f"{domanda.id_of_post}-{domanda.id_of_question}"


def _tipo(domanda: MetaculusQuestion) -> str:
    return getattr(domanda, "question_type", type(domanda).__name__)


class BotPrevisioni(FallTemplateBot2026):
    def __init__(
        self,
        *,
        impostazioni: Impostazioni,
        registro: Registro,
        modelli_previsione: list[LlmRegistrato],
        modello_ricerca: LlmRegistrato,
        modello_riassunto: LlmRegistrato,
        ricercatore: Ricercatore,
        torneo: str | None = None,
        rifai_domande_gia_previste: bool = False,
        **kwargs,
    ) -> None:
        kwargs.setdefault("research_reports_per_question", 1)
        kwargs.setdefault("predictions_per_research_report", impostazioni.previsioni_per_domanda)
        kwargs.setdefault("use_research_summary_to_forecast", False)
        kwargs.setdefault("enable_summarize_research", False)  # risparmia una chiamata per domanda
        kwargs.setdefault("skip_previously_forecasted_questions", True)
        kwargs.setdefault("extra_metadata_in_explanation", True)
        kwargs.setdefault("required_successful_predictions", 0.6)  # almeno 3 su 5
        super().__init__(
            publish_reports_to_metaculus=impostazioni.invia_previsioni,
            llms={
                "default": modelli_previsione[0],
                "summarizer": modello_riassunto,
                "researcher": modello_ricerca,
                "parser": modello_riassunto,
            },
            **kwargs,
        )
        self.imp = impostazioni
        self.registro = registro
        self.modelli_previsione = modelli_previsione
        self.ricercatore = ricercatore
        self.torneo = torneo
        self.rifai_domande_gia_previste = rifai_domande_gia_previste
        self._semaforo = asyncio.Semaphore(max(1, impostazioni.domande_in_parallelo))

    # ------------------------------------------------------ ciclo domande
    async def forecast_questions(self, questions, return_exceptions: bool = False):
        if not self.rifai_domande_gia_previste:
            nuove = [
                q for q in questions
                if not self.registro.gia_prevista(chiave_domanda(q), inviata=self.publish_reports_to_metaculus)
            ]
            if len(nuove) != len(questions):
                logger.info(f"Salto {len(questions) - len(nuove)} domande già previste (registro locale)")
            questions = nuove
        return await super().forecast_questions(questions, return_exceptions)

    async def _run_individual_question(self, question: MetaculusQuestion) -> ForecastReport:
        chiave = chiave_domanda(question)
        async with self._semaforo:
            segno = domanda_corrente.set(chiave)
            try:
                self.registro.controlla_tetti(None)
                report = await super()._run_individual_question(question)
            except BaseException as errore:
                self.registro.registra_previsione(
                    chiave, question.page_url, question.question_text, self.torneo, _tipo(question),
                    None, inviata=False, errore=f"{type(errore).__name__}: {str(errore)[:500]}",
                )
                raise
            finally:
                domanda_corrente.reset(segno)
        self.registro.registra_previsione(
            chiave, question.page_url, question.question_text, self.torneo, _tipo(question),
            _in_dati(report.prediction), inviata=self.publish_reports_to_metaculus,
        )
        return report

    async def run_research(self, question: MetaculusQuestion) -> str:
        ricerca = await self.ricercatore.ricerca(question)
        logger.info(f"Ricerca per {question.page_url}: {len(ricerca)} caratteri")
        return ricerca

    async def _modello_di_turno(self, question: MetaculusQuestion) -> LlmRegistrato:
        try:
            tentativi = (await self._get_notepad(question)).total_predictions_attempted
        except ValueError:
            tentativi = 1
        return self.modelli_previsione[(tentativi - 1) % len(self.modelli_previsione)]

    async def _ragiona(self, question: MetaculusQuestion, prompt: str) -> tuple[str, str]:
        modello = await self._modello_di_turno(question)
        testo = await modello.invoke(prompt)
        return testo, modello.model

    def _intestazione(self, question: MetaculusQuestion, research: str) -> str:
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        chiusura = question.close_time.strftime("%Y-%m-%d") if question.close_time else "unknown"
        risoluzione = (
            question.scheduled_resolution_time.strftime("%Y-%m-%d") if question.scheduled_resolution_time else "unknown"
        )
        return clean_indents(
            f"""
            You are an expert superforecaster competing in a forecasting tournament scored with log scores.

            Question: {question.question_text}

            Background:
            {question.background_info or "(none)"}

            Resolution criteria (not yet satisfied):
            {question.resolution_criteria or "(none)"}

            Fine print:
            {question.fine_print or "(none)"}

            Today: {oggi}. Question closes: {chiusura}. Scheduled resolution: {risoluzione}.

            Research notes from your assistants:
            {research}
            {METODO}
            {self._get_conditional_disclaimer_if_necessary(question)}
            """
        )

    # ------------------------------------------------------------ sì/no
    async def _run_forecast_on_binary(self, question: BinaryQuestion, research: str) -> ReasonedPrediction[float]:
        prompt = self._intestazione(question, research) + clean_indents(
            """
            Answer format: probability
            Write your reasoning following steps 1-6, then end with exactly one line:
            Probability: ZZ%
            """
        )
        testo, modello = await self._ragiona(question, prompt)
        try:
            p = combina.leggi_probabilita(testo)
        except combina.RispostaIlleggibile:
            letto: BinaryPrediction = await structure_output(
                testo, BinaryPrediction, model=self.get_llm("parser", "llm"),
                additional_instructions=self._create_resolved_question_parsing_message(),
            )
            p = combina.limita_probabilita(letto.prediction_in_decimal)
        return ReasonedPrediction(prediction_value=p, reasoning=f"[{modello}]\n{testo}")

    # -------------------------------------------------- scelta multipla
    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        elenco = "\n".join(f"- Option: {o}" for o in question.options)
        prompt = self._intestazione(question, research) + clean_indents(
            f"""
            Answer format: option probabilities
            The options are:
            {elenco}
            Leave some probability on every option to cover surprises.
            Write your reasoning following steps 1-6, then end with one line per option, in this order,
            using the option names exactly as written above:
            Option name: ZZ%
            The probabilities must sum to 100%.
            """
        )
        testo, modello = await self._ragiona(question, prompt)
        try:
            prob = combina.leggi_opzioni(testo, question.options)
        except combina.RispostaIlleggibile:
            letto: PredictedOptionList = await structure_output(
                testo, PredictedOptionList, model=self.get_llm("parser", "llm"),
                additional_instructions=f"Option names must be exactly: {question.options}",
            )
            prob = combina.normalizza_opzioni({o.option_name: o.probability for o in letto.predicted_options})
        lista = PredictedOptionList(
            predicted_options=[PredictedOption(option_name=o, probability=prob[o]) for o in question.options]
        )
        return ReasonedPrediction(prediction_value=lista, reasoning=f"[{modello}]\n{testo}")

    # -------------------------------------------- numeriche e date
    def _limiti_testo(self, question: NumericQuestion | DateQuestion) -> str:
        if isinstance(question, DateQuestion):
            basso = question.lower_bound.date().isoformat()
            alto = question.upper_bound.date().isoformat()
            unita = ""
        else:
            basso = question.nominal_lower_bound if question.nominal_lower_bound is not None else question.lower_bound
            alto = question.nominal_upper_bound if question.nominal_upper_bound is not None else question.upper_bound
            unita = question.unit_of_measure or ""
        righe = [f"Lower bound: {basso} {unita}".rstrip(), f"Upper bound: {alto} {unita}".rstrip()]
        righe.append(
            "The outcome can be below the lower bound." if question.open_lower_bound
            else "The outcome cannot be below the lower bound."
        )
        righe.append(
            "The outcome can be above the upper bound." if question.open_upper_bound
            else "The outcome cannot be above the upper bound."
        )
        return "\n".join(righe)

    async def _distribuzione(self, question: NumericQuestion | DateQuestion, research: str) -> ReasonedPrediction[NumericDistribution]:
        data = isinstance(question, DateQuestion)
        if data:
            formato = "YYYY-MM-DD"
            istruzioni = "Write dates as YYYY-MM-DD, earliest at percentile 10."
        else:
            formato = "XX"
            unita = question.unit_of_measure or "not stated (infer it)"
            istruzioni = f"Units: {unita}. Write plain numbers in these units, no scientific notation, smallest at percentile 10."
        prompt = self._intestazione(question, research) + clean_indents(
            f"""
            Answer format: percentiles
            Question type: {"date" if data else "numeric"}
            {self._limiti_testo(question)}
            {istruzioni}
            Set wide 10-90 intervals: unknown unknowns are common.
            Write your reasoning following steps 1-6 (for step 1 use past values and trends), then end with:
            Percentile 10: {formato}
            Percentile 20: {formato}
            Percentile 40: {formato}
            Percentile 60: {formato}
            Percentile 80: {formato}
            Percentile 90: {formato}
            """
        )
        testo, modello = await self._ragiona(question, prompt)
        try:
            punti = combina.leggi_percentili(testo, data=data)
        except combina.RispostaIlleggibile:
            if data:
                raise
            letti: list[Percentile] = await structure_output(
                testo, list[Percentile], model=self.get_llm("parser", "llm"),
                additional_instructions=self._create_single_distribution_parsing_message(question),
            )
            punti = [(p.percentile, p.value) for p in letti]
        basso = question.lower_bound.timestamp() if data else question.lower_bound
        alto = question.upper_bound.timestamp() if data else question.upper_bound
        puliti = combina.pulisci_percentili(punti, basso, alto, question.open_lower_bound, question.open_upper_bound)
        distribuzione = NumericDistribution.from_question(
            [Percentile(percentile=p, value=v) for p, v in puliti], question
        )
        cdf = [p.percentile for p in distribuzione.get_cdf()]
        if not combina.cdf_monotona(cdf):
            raise ValueError("CDF non monotona dopo la pulizia")
        return ReasonedPrediction(prediction_value=distribuzione, reasoning=f"[{modello}]\n{testo}")

    async def _run_forecast_on_numeric(self, question: NumericQuestion, research: str):
        return await self._distribuzione(question, research)

    async def _run_forecast_on_date(self, question: DateQuestion, research: str):
        return await self._distribuzione(question, research)

    # ------------------------------------------------------ combinazione
    async def _aggregate_predictions(self, predictions, question: MetaculusQuestion):
        if isinstance(question, BinaryQuestion):
            return combina.combina_binarie([float(p) for p in predictions])
        if isinstance(question, MultipleChoiceQuestion):
            dizionari = [{o.option_name: o.probability for o in p.predicted_options} for p in predictions]
            media = combina.combina_opzioni(dizionari)
            return PredictedOptionList(
                predicted_options=[PredictedOption(option_name=o, probability=media[o]) for o in question.options]
            )
        # Numeriche, date e condizionali: mediana delle CDF della libreria, già testata.
        return await super()._aggregate_predictions(predictions, question)


def _in_dati(previsione: object) -> object:
    if isinstance(previsione, float):
        return round(previsione, 4)
    if isinstance(previsione, PredictedOptionList):
        return {o.option_name: round(o.probability, 4) for o in previsione.predicted_options}
    if isinstance(previsione, NumericDistribution):
        punti = previsione.declared_percentiles
        if len(punti) > 10:
            punti = previsione.get_percentiles_at_target_heights()
        if previsione.is_date:
            return {
                f"p{round(p.percentile * 100)}": datetime.fromtimestamp(p.value, tz=timezone.utc).strftime("%Y-%m-%d")
                for p in punti
            }
        return {f"p{round(p.percentile * 100)}": round(p.value, 4) for p in punti}
    return str(previsione)


__all__ = ["BotPrevisioni", "TettoSuperato", "chiave_domanda"]
