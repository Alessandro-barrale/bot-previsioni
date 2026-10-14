"""Domande di esempio per la prova offline (una per tipo). Non esistono su Metaculus."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forecasting_tools import BinaryQuestion, DateQuestion, MultipleChoiceQuestion, NumericQuestion


def domande_di_prova() -> list:
    adesso = datetime.now(timezone.utc)
    comuni = dict(
        close_time=adesso + timedelta(days=10),
        scheduled_resolution_time=adesso + timedelta(days=60),
        fine_print="Domanda di prova creata localmente.",
        background_info="Domanda di prova per verificare il bot senza inviare nulla.",
    )
    return [
        BinaryQuestion(
            question_text="Will the European Central Bank cut its deposit facility rate at its December 2026 meeting?",
            resolution_criteria="Resolves Yes if the ECB announces a cut of the deposit facility rate at the December 2026 monetary policy meeting.",
            id_of_post=-1, id_of_question=-1, page_url="prova://binaria", **comuni,
        ),
        NumericQuestion(
            question_text="What will the Brent crude oil spot price be on December 31, 2026 (USD per barrel)?",
            resolution_criteria="Resolves to the Brent spot price published by the EIA for December 31, 2026.",
            unit_of_measure="USD per barrel",
            lower_bound=30.0, upper_bound=150.0, open_lower_bound=True, open_upper_bound=True,
            id_of_post=-2, id_of_question=-2, page_url="prova://numerica", **comuni,
        ),
        MultipleChoiceQuestion(
            question_text="Which company will have the top model on the LMArena text leaderboard on December 31, 2026?",
            resolution_criteria="Resolves to the organization of the first-ranked model on the LMArena text leaderboard on December 31, 2026.",
            options=["Anthropic", "Google", "OpenAI", "Other"],
            id_of_post=-3, id_of_question=-3, page_url="prova://scelta-multipla", **comuni,
        ),
        DateQuestion(
            question_text="When will the next SpaceX Starship launch take place after October 14, 2026?",
            resolution_criteria="Resolves to the UTC date of the next Starship integrated flight test launch.",
            lower_bound=adesso, upper_bound=adesso + timedelta(days=365),
            open_lower_bound=False, open_upper_bound=True,
            id_of_post=-4, id_of_question=-4, page_url="prova://data", **comuni,
        ),
    ]
