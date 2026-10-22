"""
Il bot: estende FallTemplateBot2026 del template ufficiale.

Cosa cambia rispetto al template (v2):
- ricerca a più fonti e a più giri (bot.ricerca);
- più previsioni per domanda, a rotazione su più modelli, metà con l'angolo
  "outside view" (classe di riferimento) e metà "inside view" (meccanismi);
- prompt v2: status quo e resolution check prima di tutto, base rate calcolata
  e "anchor on your math", regole soft clock / history discharged / null
  result, domande multi-clausola come prodotto booleano, auto-controllo di
  calibrazione; numeriche e date con 13 percentili e note sul log density score;
- ogni risposta chiude con un blocco JSON obbligatorio (regex come riserva,
  parser LLM come ultima riserva);
- combinazione: mediana (sì/no), media o mediana per opzione (scelta multipla),
  mediana punto per punto delle CDF (numeriche e date);
- limiti: sì/no in [0.02, 0.98], pavimento 0.01 per opzione;
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

# ------------------------------------------------------------------ prompt v2
METODO = """
## How you must work (in this order)

1. STATUS QUO FIRST. This question is open and unresolved as of today. If nothing changed between
   now and resolution, how would it resolve? Commit explicitly: either write "no qualifying event has
   yet occurred inside the window" or name the in-window trigger and its date. To move away from the
   status quo you must name the specific event, inside the window, with a date, that would change it.

2. RESOLUTION CHECK. Read the resolution criteria and fine print literally: the question resolves on
   what they say, not on the spirit of the title. If the research shows the criterion is ALREADY
   satisfied (or already impossible) as of today, say so, forecast >= 95% (or <= 5%) and stop the
   analysis there. Otherwise continue.

3. BASE RATE, CALCULATED. The relevant base rate is the historical frequency for questions LIKE THIS
   ONE (same actor, same kind of event, same horizon), not a generic "most things don't happen" prior.
   Compute a number from data: a frequency, a hazard rate applied to the exposure that REMAINS (not to
   the whole window if part of it has already passed without events), or a product of clauses. A
   rough quantitative estimate from data is more reliable than an intuitive guess.
   ANCHOR ON YOUR MATH: once you have that number, moving more than 15 percentage points away from
   it requires a NEW and NAMED piece of evidence (what, from whom, dated). "30% because this is a
   novel situation" is not allowed.

4. THREE RULES THAT OTHER BOTS GOT WRONG (measured losses):
   - SOFT CLOCK: an announced date (launch, vote, release, meeting, deadline) is evidence that a goal
     EXISTS, not that it will HOLD. Price "P(the date holds)" from that actor's record of slips, never
     near 1 just because the date was announced. Do not decompose P(date holds) x P(event | date) with
     the first term near 1.
   - HISTORY DISCHARGED: if the mechanism that produced the historical cadence has stopped (the
     program ended, the law changed, the person left, the series was discontinued), the historical
     rate is a CEILING, not the center of your forecast.
   - NULL RESULT: a search that finds nothing authorizes only "we found no evidence of X", never
     "X does not exist" or "X did not happen". Weigh how likely the evidence would be to show up in
     the research if X were true.

5. MULTI-CLAUSE QUESTIONS. If the criteria require several things at once, write them as a
   Boolean product: "Yes iff A x B x C = 1". Give one Yes-world and one No-world example. Then
   write a small table with one probability per clause (conditional on the previous ones) and the
   product. Your final number must be anchored on that product.

6. EVIDENCE. For each fact in the research, check its DATE and its source. Prefer the resolution
   source itself (what it says NOW) over commentary. If a liquid prediction market on the same
   criteria exists, treat its price as a strong estimate you may leave only by naming a mismatch
   (criteria, date, liquidity); a thin market is weak evidence, never an anchor.

7. CALIBRATION SELF-CHECK, before the final answer:
   - "Out of 100 situations like this, my forecast says the event happens in X of them." Does X make
     sense given the base rate and the time remaining?
   - Name the one scenario most likely to make this forecast look silly in hindsight. Does its
     probability fit the number you are giving?
   - 90% is 9:1 odds, 95% is 19:1, 99% is 99:1. Would you take the other side of that bet?
   - Do not hedge towards 50% without a reason; do not go extreme without the resolution check.

Scoring: log score against the other bots on the same question. Overconfidence on a miss costs far
more than an honest 70% does. Keep the final answer between 2% and 98% unless step 2 applies.
"""

ANGOLO_OUTSIDE = """
## Your assigned lens for this sample: OUTSIDE VIEW
Start from the reference class and its statistics. Spend most of your reasoning on steps 1-3:
how often did questions like this one resolve Yes (or land where), over the same horizon? Use the
specifics of this case only to adjust that number, and say by how much and why.
"""

ANGOLO_INSIDE = """
## Your assigned lens for this sample: INSIDE VIEW
Start from the mechanisms of this specific case: who has to do what, by when, with what incentives
and obstacles, and what the resolution source says now. Build the causal chain to each outcome, then
check the resulting number against the base rate of step 3 and explain any gap of more than 15 points.
"""

FORMATO_JSON = """
Finish with ONE final JSON block (it is parsed by a program; nothing may follow it):
```json
{schema}
```
"""

SCORING_NUMERICO = """
How continuous questions are scored: a log density score over 201 points of your CDF between the
bounds. Mass beyond an OPEN bound is scored as its own outcome against a reference of a few percent,
so starving it is heavily punished; a CLOSED bound cannot be exceeded and should receive no mass. This
is a proper scoring rule: to maximize expected score, report your true uncertainty and resist
overconfident, narrow shapes, but do not pad the tails without a reason either.

How wide: an administered or slow-moving series (official statistics, policy rates, scheduled
counts) is largely predictable from its latest value and historical variance: anchor tightly on the
latest observation with its date. A traded price, a volatile count or a novel metric on a short
horizon is close to a random walk: center on the current value, take the width from its realized
variability over windows as long as the time remaining, and do not expect movement you cannot source
to a named cause.
"""

SCORING_DATE = """
Dates: "open" means the event has not happened yet, so the status quo is that it does NOT happen
within the displayed window; every date you place earlier than the upper bound is a claim that
something changes, and the mass you put beyond the upper bound is your probability that it does not
happen inside the window at all (if the upper bound is open). Announced target dates slip: price the
slip from the actor's record (soft clock rule).
"""


class ChiusuraImminente(RuntimeError):
    """La domanda chiude tra pochissimo: non si spende per una previsione che non arriverebbe in tempo."""


def minuti_alla_chiusura(domanda: MetaculusQuestion, adesso: datetime | None = None) -> float | None:
    if domanda.close_time is None:
        return None
    adesso = adesso or datetime.now(timezone.utc)
    chiusura = domanda.close_time if domanda.close_time.tzinfo else domanda.close_time.replace(tzinfo=timezone.utc)
    return (chiusura - adesso).total_seconds() / 60


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
        kwargs.setdefault("required_successful_predictions", 0.5)  # almeno 3 su 5, 3 su 6: un vendor può cadere
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
        minuti = minuti_alla_chiusura(question)
        veloce = False
        if minuti is not None:
            if minuti < self.imp.minuti_salto:
                raise ChiusuraImminente(f"chiude tra {minuti:.0f} minuti (sotto i {self.imp.minuti_salto}): saltata")
            veloce = minuti < self.imp.minuti_via_veloce
            if veloce:
                logger.info(f"{question.page_url}: chiude tra {minuti:.0f} minuti, via veloce (solo web e mercati)")
        ricerca = await self.ricercatore.ricerca(question, veloce=veloce)
        logger.info(f"Ricerca per {question.page_url}: {len(ricerca)} caratteri")
        return ricerca

    # ------------------------------------------------- rotazione e angoli
    async def _numero_tentativo(self, question: MetaculusQuestion) -> int:
        try:
            return (await self._get_notepad(question)).total_predictions_attempted
        except ValueError:
            return 1

    async def _modello_di_turno(self, question: MetaculusQuestion) -> LlmRegistrato:
        tentativo = await self._numero_tentativo(question)
        return self.modelli_previsione[(tentativo - 1) % len(self.modelli_previsione)]

    @staticmethod
    def angolo(tentativo: int) -> str:
        """Campioni dispari: outside view; pari: inside view. Metà e metà, stessa ricerca."""
        return "outside" if tentativo % 2 == 1 else "inside"

    async def _ragiona(self, question: MetaculusQuestion, prompt: str) -> tuple[str, str]:
        modello = await self._modello_di_turno(question)
        testo = await modello.invoke(prompt)
        return testo, modello.model

    async def _intestazione(self, question: MetaculusQuestion, research: str) -> str:
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        chiusura = question.close_time.strftime("%Y-%m-%d %H:%M UTC") if question.close_time else "unknown"
        risoluzione = (
            question.scheduled_resolution_time.strftime("%Y-%m-%d") if question.scheduled_resolution_time else "unknown"
        )
        angolo = self.angolo(await self._numero_tentativo(question))
        testo_angolo = ANGOLO_OUTSIDE if angolo == "outside" else ANGOLO_INSIDE
        return clean_indents(
            f"""
            You are an expert superforecaster competing in a forecasting tournament for bots, scored
            with log scores against the other bots on the same question.

            Question: {question.question_text}

            Background:
            {question.background_info or "(none)"}

            Resolution criteria:
            {question.resolution_criteria or "(none)"}

            Fine print:
            {question.fine_print or "(none)"}

            Today: {oggi}. Question closes: {chiusura}. Scheduled resolution: {risoluzione}.

            Research notes from your assistants (each item shows its source and date when known):
            {research}
            {METODO}
            {testo_angolo}
            {self._get_conditional_disclaimer_if_necessary(question)}
            """
        )

    # ------------------------------------------------------------ sì/no
    async def _run_forecast_on_binary(self, question: BinaryQuestion, research: str) -> ReasonedPrediction[float]:
        prompt = await self._intestazione(question, research) + clean_indents(
            """
            Answer format: probability
            Write your reasoning following steps 1-7. If you still want a line of text at the end, write
            "Probability: ZZ%" as the last line BEFORE the JSON block.
            """
        ) + FORMATO_JSON.format(schema='{"probability": 0.37}') + (
            "The probability is a decimal between 0.02 and 0.98 (0.95 or more / 0.05 or less only when the\n"
            "resolution check of step 2 applies). The JSON block must be the very last thing you write.\n"
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
        schema = "{" + '"option_probabilities": {' + ", ".join(f'"{o}": 0.0' for o in question.options) + "}}"
        prompt = await self._intestazione(question, research) + clean_indents(
            f"""
            Answer format: option probabilities
            The options are:
            {elenco}
            Apply steps 1-7 to the set of options: status quo option first, then the base rate of each
            option, then what would have to happen for each other option. Leave at least 1% on every
            option to cover surprises; the probabilities must sum to 1.
            """
        ) + FORMATO_JSON.format(schema=schema) + (
            "Use the option names exactly as written above, decimals that sum to 1.\n"
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
            "The lower bound is OPEN: the outcome can be below it, and your percentiles may go below it."
            if question.open_lower_bound
            else "The lower bound is CLOSED: the outcome cannot be below it."
        )
        righe.append(
            "The upper bound is OPEN: the outcome can be above it, and your percentiles may go above it."
            if question.open_upper_bound
            else "The upper bound is CLOSED: the outcome cannot be above it."
        )
        return "\n".join(righe)

    async def _distribuzione(self, question: NumericQuestion | DateQuestion, research: str) -> ReasonedPrediction[NumericDistribution]:
        data = isinstance(question, DateQuestion)
        livelli = combina.PERCENTILI_V2
        if data:
            istruzioni = "Write every value as a date YYYY-MM-DD (UTC), strictly increasing from percentile 1 to 99."
            esempio = "2027-03-15"
            scoring = SCORING_DATE
        else:
            unita = question.unit_of_measure or "not stated (infer it from the question and the bounds)"
            istruzioni = (
                f"Units: {unita}. Write plain numbers in these base units (1000000, not 1e6 or 1,000,000 or "
                "'1 million'), strictly increasing from percentile 1 to 99."
            )
            esempio = "0"
            scoring = SCORING_NUMERICO
        schema = "{" + '"declared_percentiles": {' + ", ".join(f'"{p:g}": {esempio!r}' if data else f'"{p:g}": {esempio}' for p in livelli) + "}}"
        schema = schema.replace("'", '"')
        prompt = await self._intestazione(question, research) + clean_indents(
            f"""
            Answer format: percentiles
            Question type: {"date" if data else "numeric"}
            {self._limiti_testo(question)}
            {scoring}
            Write your reasoning following steps 1-7 (for step 3 use the latest value with its date, past
            values and their variability over windows as long as the time remaining). Then give your
            distribution as 13 percentiles: 1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5 and 99.
            {istruzioni}
            """
        ) + FORMATO_JSON.format(schema=schema)
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
        # Numeriche, date e condizionali: mediana punto per punto delle CDF
        # (forecasting-tools; il test test_combina.test_numeriche_mediana_delle_cdf_non_dei_percentili
        # verifica che non sia la media dei percentili).
        return await super()._aggregate_predictions(predictions, question)


def _in_dati(previsione: object) -> object:
    if isinstance(previsione, float):
        return round(previsione, 4)
    if isinstance(previsione, PredictedOptionList):
        return {o.option_name: round(o.probability, 4) for o in previsione.predicted_options}
    if isinstance(previsione, NumericDistribution):
        punti = previsione.declared_percentiles
        if len(punti) > len(combina.PERCENTILI_V2):
            punti = previsione.get_percentiles_at_target_heights([p / 100 for p in combina.PERCENTILI_V2])
        if previsione.is_date:
            return {
                f"p{p.percentile * 100:g}": datetime.fromtimestamp(p.value, tz=timezone.utc).strftime("%Y-%m-%d")
                for p in punti
            }
        return {f"p{p.percentile * 100:g}": round(p.value, 4) for p in punti}
    return str(previsione)


__all__ = ["BotPrevisioni", "ChiusuraImminente", "TettoSuperato", "chiave_domanda", "minuti_alla_chiusura"]
