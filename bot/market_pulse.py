"""
Market Pulse 26Q4 (Metaculus, 7.500 $, bot ammessi ai premi): domande di gruppo
numeriche su ricavi, utili per azione e guidance di grandi aziende, che si aprono
a rotazione dal 7/10 al 31/12/2026 (ultimo giorno utile per i premi: 21/12).

Domande. forecasting-tools 0.3.4 spacchetta già i gruppi:
get_all_open_questions_from_tournament(slug) usa group_question_mode =
"unpack_subquestions" e restituisce una NumericQuestion per ogni sotto-domanda
APERTA (lo stato è filtrato sotto-domanda per sotto-domanda), con
group_question_option = etichetta e api_json = il post con la sotto-domanda al
posto di "question". La libreria però scarta il gruppo intero se manca un campo
del gruppo (fine_print, description, resolution_criteria: KeyError), sostituisce
i criteri della sotto-domanda con quelli del gruppo e aspetta 3,5-4,5 secondi
prima di ogni richiesta. Per questo prima si leggono i post direttamente
(/api/posts/?tournaments=slug&statuses=open, una richiesta) con uno spacchettatore
tollerante: campi del gruppo mancanti, criteri sulla sotto-domanda, stato o
spot_scoring_time assenti, post non di gruppo. La libreria si chiama solo se la
lettura diretta fallisce o non riesce a leggere qualche sotto-domanda, e le due
liste si uniscono senza doppioni (vince la lettura diretta). Così il giro senza
niente da fare resta di pochi secondi.

Calendario (costo controllato). Conta solo la previsione in piedi allo spot
scoring di ogni sotto-domanda (spot_scoring_time, o la chiusura se manca):
- prima previsione quando la sotto-domanda compare;
- una seconda nelle ultime 6 ore prima dello spot scoring, solo se la prima ha
  almeno 1 giorno;
- niente altre ripetizioni.
Le previsioni riuscite si leggono dal registro (filtrate per torneo) e, quando si
invia davvero, anche dalla storia delle nostre previsioni nell'API: se il registro
non fosse stato salvato, non si rifà la prima.

Tetti: mensile e giornaliero propri (TETTO_MESE_MP_USD, TETTO_GIORNO_MP_USD),
contati sul registro principale filtrando per torneo e dentro i tetti generali; il
tetto per domanda vale per ogni passata, con il doppio sul totale della domanda.
Al massimo MP_DOMANDE_PER_GIRO sotto-domande per giro (prima gli aggiornamenti).
Le ultime 2 x TETTO_DOMANDA_USD di ogni tetto proprio (al massimo un quarto del
tetto) sono una riserva per gli aggiornamenti: quando la spesa ci arriva, le prime
previsioni aspettano il giorno (o il mese) dopo, gli aggiornamenti no.

Il bot entra in Market Pulse una volta sola, con il token del bot principale:
main.py rifiuta --modo market_pulse con --bot 2.
"""
from __future__ import annotations

import copy
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from forecasting_tools import DateQuestion, MetaculusQuestion, NumericQuestion
from forecasting_tools.data_models.data_organizer import DataOrganizer

from bot.previsore import BotPrevisioni, chiave_domanda
from template_bot import FallTemplateBot2026

logger = logging.getLogger(__name__)

NOME_TORNEO = "market_pulse"  # etichetta nel registro (colonna torneo di previsioni e chiamate)
ORE_FINESTRA = 6  # aggiornamento solo nelle ultime 6 ore prima dello spot scoring
GIORNI_MINIMI = 1  # ... e solo se la prima previsione ha almeno 1 giorno
PASSATE_MASSIME = 2  # prima previsione + un aggiornamento
RISERVA_AGGIORNAMENTI = 2  # domande (a TETTO_DOMANDA_USD l'una) tenute da parte per gli aggiornamenti
API = "https://www.metaculus.com/api"
TIMEOUT = 30

PRIMA = "prima"
AGGIORNAMENTO = "aggiornamento"

# Priorità del primo giro di ricerca web per queste domande (testo per il modello, in inglese).
GUIDA_WEB = """This question is about a listed company's quarterly results. Find, with sources and dates:
1. the latest analyst consensus for exactly this metric and fiscal period (mean, range, number of analysts,
   revisions over the last 90 days);
2. the company's own guidance for that period (range, midpoint, date it was given);
3. the scheduled date of the earnings report;
4. the reported figure vs the consensus for this metric in each of the last 8 quarters (beat or miss in %),
   and vs the guidance midpoint;
5. anything since the last report that could move the figure (pre-announcements, segment data, supply,
   prices, macro)."""

NOTA_TRIMESTRALI = """
Quarterly company results (Market Pulse). The reported quarterly revenue of a large company is an
ADMINISTERED series, not a random walk: management guides it, analysts converge on a consensus, and the
reported figure usually lands close to that consensus, more often slightly above it than below.
- Center: the latest analyst consensus for exactly this metric and fiscal period, or the midpoint of the
  company's own guidance if that is newer; then shift by this company's typical surprise.
- Width: from this company's history of surprises (beats and misses in % against consensus over the last
  8 quarters, from the research), not from share-price volatility. Revenue surprises of large companies
  are usually a few percent; EPS, margins, segment figures and guidance surprise more.
- Guidance questions (the range the company will give for a later quarter): anchor on the current
  consensus for that later quarter and on how this company usually guides relative to it.
- If the figure has already been reported, apply the resolution check (step 2) and go narrow around it.
- Leave real probability mass beyond every OPEN bound: under the log score a starved tail costs far more
  than a slightly wider body.
"""


# ------------------------------------------------------------ campi della sotto-domanda
def _istante(valore) -> datetime | None:
    """Data dell'API: stringa ISO (con Z) o secondi epoch."""
    if valore is None or valore == "":
        return None
    if isinstance(valore, datetime):
        return valore if valore.tzinfo else valore.replace(tzinfo=timezone.utc)
    if isinstance(valore, (int, float)) and not isinstance(valore, bool):
        return datetime.fromtimestamp(valore, tz=timezone.utc)
    try:
        istante = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except ValueError:
        return None
    return istante if istante.tzinfo else istante.replace(tzinfo=timezone.utc)


def _json_sottodomanda(domanda: MetaculusQuestion) -> dict:
    """Il JSON della sotto-domanda: il "question" del post (forma spacchettata) o la voce del gruppo con lo stesso id."""
    post = domanda.api_json or {}
    questione = post.get("question")
    if isinstance(questione, dict) and questione.get("id") in (None, domanda.id_of_question):
        return questione
    for sotto in ((post.get("group_of_questions") or {}).get("questions")) or []:
        if isinstance(sotto, dict) and sotto.get("id") == domanda.id_of_question:
            return sotto
    return {}


def momento_spot(domanda: MetaculusQuestion) -> datetime | None:
    return _istante(_json_sottodomanda(domanda).get("spot_scoring_time"))


def scadenza(domanda: MetaculusQuestion) -> datetime | None:
    """Ultimo momento utile: il primo tra spot scoring e chiusura (None se l'API non dice nessuno dei due)."""
    candidati = [m for m in (momento_spot(domanda), _istante(domanda.close_time)) if m is not None]
    return min(candidati) if candidati else None


def momenti_api(domanda: MetaculusQuestion) -> list[datetime]:
    """Inizi delle nostre previsioni secondo l'API (my_forecasts.history), dal JSON o da previous_forecasts."""
    storia = ((_json_sottodomanda(domanda).get("my_forecasts") or {}).get("history")) or []
    momenti = [m for m in (_istante(v.get("start_time")) for v in storia if isinstance(v, dict)) if m is not None]
    if not momenti:
        for precedente in getattr(domanda, "previous_forecasts", None) or []:
            m = _istante(getattr(precedente, "timestamp", None))
            if m is not None:
                momenti.append(m)
    return sorted(momenti)


def testo_completo(domanda: MetaculusQuestion) -> str:
    """Testo della sotto-domanda con il titolo del gruppo e l'etichetta, se il titolo da solo non basta."""
    testo = (domanda.question_text or "").strip()
    etichetta = (domanda.group_question_option or "").strip()
    titolo_gruppo = str((domanda.api_json or {}).get("title") or "").strip()
    base = testo if testo and testo.lower() != etichetta.lower() else (titolo_gruppo or testo)
    if etichetta and etichetta.lower() not in base.lower():
        base = f"{base} — {etichetta}"
    return base


def arricchisci(domanda: MetaculusQuestion) -> MetaculusQuestion:
    """Testo completo e contesto del gruppo nello sfondo (una volta sola per oggetto)."""
    if domanda.custom_metadata.get("market_pulse_arricchita"):
        return domanda
    post = domanda.api_json or {}
    titolo_gruppo = str(post.get("title") or "").strip()
    etichetta = (domanda.group_question_option or "").strip()
    fratelli = [
        str(s.get("label") or s.get("title") or "").strip()
        for s in ((post.get("group_of_questions") or {}).get("questions")) or []
        if isinstance(s, dict) and s.get("id") != domanda.id_of_question
    ]
    fratelli = [f for f in fratelli if f][:15]
    domanda.question_text = testo_completo(domanda)
    righe = []
    if titolo_gruppo and (etichetta or fratelli):
        righe.append(f'Market Pulse question group: "{titolo_gruppo}".')
        if etichetta:
            righe.append(f'This sub-question is about: "{etichetta}".')
        if fratelli:
            righe.append("Other sub-questions in the same group: " + "; ".join(fratelli) + ".")
    if righe:
        domanda.background_info = "\n".join(righe) + ("\n\n" + domanda.background_info if domanda.background_info else "")
    domanda.custom_metadata["market_pulse_arricchita"] = True
    return domanda


# ------------------------------------------------------------ lettura delle domande
def _unisci_testi(del_gruppo, della_sotto) -> str:
    gruppo = str(del_gruppo or "").strip()
    sotto = str(della_sotto or "").strip()
    if gruppo and sotto and sotto not in gruppo:
        return f"{gruppo}\n\n{sotto}"
    return gruppo or sotto


def _stato(sotto: dict, post: dict, adesso: datetime) -> str:
    if sotto.get("status"):
        return str(sotto["status"])
    apertura, chiusura = _istante(sotto.get("open_time")), _istante(sotto.get("scheduled_close_time"))
    if apertura is not None and chiusura is not None:
        return "upcoming" if adesso < apertura else "closed" if adesso >= chiusura else "open"
    return str(post.get("status") or "open")


def _post_base(post: dict) -> dict:
    """Il post senza "question", con default_project sempre presente (la libreria lo legge se c'è "projects")."""
    base = {k: v for k, v in post.items() if k != "question"}
    progetti = base.get("projects")
    if isinstance(progetti, dict) and not isinstance(progetti.get("default_project"), dict):
        base["projects"] = {**progetti, "default_project": {"id": None}}
    return base


def _prepara(questione: dict) -> dict:
    if "type" not in questione and isinstance(questione.get("scaling"), dict):
        questione["type"] = "numeric"
    if isinstance(questione.get("scaling"), dict):
        questione["scaling"].setdefault("zero_point", None)
    return questione


def estrai_domande(post: dict, adesso: datetime | None = None, errori: list[str] | None = None) -> list[MetaculusQuestion]:
    """
    Domande di un post dell'API, tollerante: gruppo (group_of_questions) con i testi del
    gruppo, della sotto-domanda o di entrambi, oppure post con una sola domanda.
    Una sotto-domanda illeggibile viene saltata con un avviso (e annotata in `errori`).
    """
    errori = errori if errori is not None else []
    adesso = adesso or datetime.now(timezone.utc)
    if not isinstance(post, dict) or "notebook" in post:
        return []
    gruppo = post.get("group_of_questions")
    if isinstance(gruppo, dict):
        sotto_domande = [s for s in gruppo.get("questions") or [] if isinstance(s, dict)]
        ids = [s.get("id") for s in sotto_domande if s.get("id") is not None]
        domande = []
        for sotto in sotto_domande:
            nuova = _prepara(copy.deepcopy(sotto))
            for campo in ("description", "resolution_criteria", "fine_print"):
                nuova[campo] = _unisci_testi(gruppo.get(campo), sotto.get(campo))
            nuova["status"] = _stato(sotto, post, adesso)
            nuovo_post = _post_base(post)
            nuovo_post["question"] = nuova
            try:
                domanda = DataOrganizer.get_question_from_post_json(nuovo_post)
            except Exception as errore:
                errori.append(f"{post.get('id')}-{sotto.get('id')}: {type(errore).__name__}")
                logger.warning(f"Market Pulse: sotto-domanda {sotto.get('id')} del post {post.get('id')} illeggibile: {type(errore).__name__}: {errore}")
                continue
            domanda.question_ids_of_group = list(ids)
            domande.append(domanda)
        return domande
    if isinstance(post.get("question"), dict):
        nuovo_post = _post_base(post)
        nuovo_post["question"] = _prepara(copy.deepcopy(post["question"]))
        nuovo_post["question"]["status"] = _stato(nuovo_post["question"], post, adesso)
        try:
            return [DataOrganizer.get_question_from_post_json(nuovo_post)]
        except Exception as errore:
            errori.append(f"{post.get('id')}: {type(errore).__name__}")
            logger.warning(f"Market Pulse: post {post.get('id')} illeggibile: {type(errore).__name__}: {errore}")
    return []


def scarica_post_aperti(slug: str, massimo_pagine: int = 5) -> list[dict]:
    """I post aperti del torneo, letti direttamente dall'API con il token del bot (mai stampato)."""
    token = os.getenv("METACULUS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("manca METACULUS_TOKEN")
    intestazioni = {"Authorization": f"Token {token}", "Accept-Language": "en"}
    post: list[dict] = []
    for pagina in range(massimo_pagine):
        risposta = requests.get(
            f"{API}/posts/",
            headers=intestazioni,
            params={"tournaments": slug, "statuses": "open", "limit": 100, "offset": 100 * pagina},
            timeout=TIMEOUT,
        )
        risposta.raise_for_status()
        risultati = risposta.json().get("results") or []
        post += risultati
        if len(risultati) < 100:
            break
    return post


def _aperta(domanda: MetaculusQuestion) -> bool:
    return domanda.state is not None and domanda.state.value == "open"


def domande_aperte(client, slug: str, scarica_post=None) -> list[MetaculusQuestion]:
    """
    Sotto-domande aperte del torneo. Prima la lettura diretta (una richiesta); la libreria
    (gruppi spacchettati da forecasting-tools) solo se la lettura diretta fallisce o lascia
    indietro qualche sotto-domanda; le due liste si uniscono per chiave. Errore solo se
    falliscono entrambe.
    """
    raccolte: dict[str, MetaculusQuestion] = {}
    illeggibili: list[str] = []
    errore_diretta: str | None = None
    posts: list[dict] = []
    try:
        posts = list((scarica_post or scarica_post_aperti)(slug))
        for post in posts:
            for domanda in estrai_domande(post, errori=illeggibili):
                if _aperta(domanda):
                    raccolte[chiave_domanda(domanda)] = domanda
    except Exception as errore:
        errore_diretta = f"{type(errore).__name__}: {str(errore)[:200]}"
    dirette = len(raccolte)
    if errore_diretta is None and not illeggibili and (raccolte or not posts):
        logger.info(f"Market Pulse {slug}: {dirette} sotto-domande aperte in {len(posts)} post (lettura diretta)")
        return list(raccolte.values())
    motivo = f"lettura diretta fallita ({errore_diretta})" if errore_diretta else (
        f"{len(illeggibili)} sotto-domande illeggibili" if illeggibili else "nessuna sotto-domanda aperta letta"
    )
    try:
        da_libreria = [d for d in client.get_all_open_questions_from_tournament(slug) if _aperta(d)]
    except Exception as errore:
        if errore_diretta is not None:
            raise RuntimeError(
                f"Market Pulse: domande non lette (lettura diretta {errore_diretta}; libreria {type(errore).__name__}: {str(errore)[:200]})"
            ) from errore
        logger.warning(f"Market Pulse: anche la libreria è fallita ({type(errore).__name__}); uso la lettura diretta")
        da_libreria = []
    for domanda in da_libreria:
        raccolte.setdefault(chiave_domanda(domanda), domanda)  # a parità di chiave vince la lettura diretta
    logger.info(
        f"Market Pulse {slug}: {len(raccolte)} sotto-domande aperte (lettura diretta {dirette}, libreria {len(da_libreria)}; "
        f"libreria chiamata perché: {motivo})"
    )
    return list(raccolte.values())


# ------------------------------------------------------------ calendario
def decidi_fase(
    fine: datetime | None, momenti: list[datetime], adesso: datetime, minuti_minimi: float = 0
) -> tuple[str | None, str]:
    """
    (fase, motivo). fase = PRIMA, AGGIORNAMENTO o None (niente da fare in questo giro).
    `fine` è lo spot scoring (o la chiusura), `momenti` le nostre previsioni riuscite.
    """
    if fine is not None and fine <= adesso:
        return None, "spot scoring passato"
    if fine is not None and (fine - adesso).total_seconds() / 60 < minuti_minimi:
        return None, "troppo vicina allo spot scoring"
    if not momenti:
        return PRIMA, "prima previsione"
    if len(momenti) >= PASSATE_MASSIME:
        return None, "già aggiornata"
    if fine is None:
        return None, "nessuno spot scoring noto: solo la prima previsione"
    if fine - adesso > timedelta(hours=ORE_FINESTRA):
        return None, "aggiornamento più avanti"
    if adesso - min(momenti) < timedelta(days=GIORNI_MINIMI):
        return None, "prima previsione di meno di un giorno fa"
    return AGGIORNAMENTO, "ultime ore prima dello spot scoring"


def pianifica(
    domande: list[MetaculusQuestion],
    registro,
    inviata: bool,
    adesso: datetime | None = None,
    massimo: int | None = None,
    minuti_minimi: float = 0,
) -> list[tuple[MetaculusQuestion, str]]:
    """Le sotto-domande da fare in questo giro, prima gli aggiornamenti, poi per spot scoring più vicino."""
    adesso = adesso or datetime.now(timezone.utc)
    scelte: list[tuple[MetaculusQuestion, str, datetime | None]] = []
    motivi: dict[str, int] = {}
    for domanda in domande:
        momenti = registro.momenti_previsioni(chiave_domanda(domanda), inviata, NOME_TORNEO)
        if inviata:
            dall_api = momenti_api(domanda)
            if len(dall_api) > len(momenti):  # previsioni inviate ma non nel registro (registro non salvato)
                momenti = sorted(dall_api)
        fine = scadenza(domanda)
        fase, motivo = decidi_fase(fine, momenti, adesso, minuti_minimi)
        motivi[motivo] = motivi.get(motivo, 0) + 1
        if fase is not None:
            scelte.append((domanda, fase, fine))
    lontano = adesso + timedelta(days=3650)
    scelte.sort(key=lambda s: (s[1] != AGGIORNAMENTO, s[2] or lontano))
    rimandate = scelte[massimo:] if massimo is not None else []
    scelte = scelte[:massimo] if massimo is not None else scelte
    riepilogo = ", ".join(f"{motivo} {n}" for motivo, n in sorted(motivi.items()))
    logger.info(
        f"Market Pulse: {len(domande)} sotto-domande aperte; in questo giro {len(scelte)} "
        f"({sum(f == PRIMA for _, f, _ in scelte)} prime, {sum(f == AGGIORNAMENTO for _, f, _ in scelte)} aggiornamenti)"
        + (f", {len(rimandate)} rimandate al giro dopo" if rimandate else "")
        + (f" [{riepilogo}]" if riepilogo else "")
    )
    return [(domanda, fase) for domanda, fase, _ in scelte]


def guida_web(domanda: MetaculusQuestion) -> str:
    return GUIDA_WEB


# ------------------------------------------------------------ il bot
class BotMarketPulse(BotPrevisioni):
    """BotPrevisioni con il calendario di Market Pulse: più previsioni per domanda, solo in questo torneo."""

    passate_multiple = True

    def __init__(self, *, massimo_per_giro: int | None = None, **kwargs) -> None:
        kwargs["torneo"] = NOME_TORNEO
        # Il salto delle domande già previste lo fa il calendario, non la libreria né il registro.
        kwargs["skip_previously_forecasted_questions"] = False
        kwargs["rifai_domande_gia_previste"] = True
        super().__init__(**kwargs)
        self.massimo_per_giro = massimo_per_giro
        self._fasi: dict[str, str] = {}

    async def forecast_questions(self, questions, return_exceptions: bool = False):
        for domanda in questions:
            arricchisci(domanda)
        scelte = pianifica(
            list(questions), self.registro, inviata=self.publish_reports_to_metaculus,
            massimo=self.massimo_per_giro, minuti_minimi=self.imp.minuti_salto,
        )
        scelte = self._tieni_la_riserva(scelte)
        self._fasi = {chiave_domanda(d): fase for d, fase in scelte}
        # Si salta il filtro "già prevista" di BotPrevisioni: qui decide il calendario.
        return await FallTemplateBot2026.forecast_questions(self, [d for d, _ in scelte], return_exceptions)

    def _tieni_la_riserva(self, scelte: list[tuple[MetaculusQuestion, str]]) -> list[tuple[MetaculusQuestion, str]]:
        """Vicino a un tetto proprio si fanno solo gli aggiornamenti: sono quelli che contano allo spot scoring."""
        propri = self.registro.tetti_torneo.get(NOME_TORNEO)
        prime = sum(fase == PRIMA for _, fase in scelte)
        if propri is None or not prime:
            return scelte
        vicini = []
        for nome, tetto, spesa_di in (
            ("giornaliero", propri.giorno, self.registro.spesa_giorno_torneo),
            ("mensile", propri.mese, self.registro.spesa_mese_torneo),
        ):
            if tetto is None:
                continue
            riserva = min(RISERVA_AGGIORNAMENTI * self.imp.tetto_domanda, tetto / 4)
            if spesa_di(NOME_TORNEO) >= tetto - riserva:
                vicini.append(f"{nome} (riserva {riserva:.2f} $)")
        if not vicini:
            return scelte
        logger.info(
            f"Market Pulse: {prime} prime previsioni rimandate, vicino al tetto {' e '.join(vicini)}: "
            "il resto è per gli aggiornamenti"
        )
        return [(d, fase) for d, fase in scelte if fase == AGGIORNAMENTO]

    def _minuti_rimasti(self, question: MetaculusQuestion) -> float | None:
        fine = scadenza(question)
        if fine is None:
            return None
        return (fine - datetime.now(timezone.utc)).total_seconds() / 60

    def _nota_numerica(self, question: NumericQuestion | DateQuestion) -> str:
        nota = NOTA_TRIMESTRALI
        fine = scadenza(question)
        if fine is not None:
            nota += (
                f"Spot scoring: only the forecast standing at {fine.strftime('%Y-%m-%d %H:%M')} UTC counts for this "
                "sub-question; forecast the value as you expect it to resolve.\n"
            )
        if self._fasi.get(chiave_domanda(question)) == AGGIORNAMENTO:
            nota += (
                "This is the planned UPDATE of an earlier forecast, made in the last hours before that moment: use the "
                "newest consensus, guidance and news; if the figure has been reported, step 2 applies.\n"
            )
        return nota
