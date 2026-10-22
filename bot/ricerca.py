"""
Ricerca a più fonti e a più giri (v2).

Giro 1, in parallelo:
- la fonte di risoluzione (pagine citate nei criteri, lette ORA);
- i mercati di previsione (Polymarket, Manifold, Kalshi) abbinati da un modello economico;
- le serie storiche (FRED, Yahoo Finance) per le domande numeriche;
- AskNews (notizie recenti + archivio) e la ricerca web di Claude.
Giro 2..N: un modello economico legge le note e scrive cosa manca; quelle
domande vanno alla ricerca web.

Via veloce (poco tempo alla chiusura): solo web e mercati, un giro.

Ogni fonte riporta le date. Se una fonte fallisce si va avanti con le altre;
se falliscono tutte, si usa la conoscenza del modello di ricerca (segnalandolo).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from forecasting_tools import MetaculusQuestion, NumericQuestion, clean_indents

from bot.config import Impostazioni, chiave_presente
from bot.llm import LlmRegistrato
from bot.spese import Registro, TettoSuperato, domanda_corrente

logger = logging.getLogger(__name__)

LIMITE_ASKNEWS_MESE = 950  # il piano gratuito dà 1.000 chiamate al mese
_lucchetto_asknews = asyncio.Lock()


@dataclass
class EsitoRicerca:
    sezioni: list[str] = field(default_factory=list)
    fonti_fallite: list[str] = field(default_factory=list)
    fonti_usate: list[str] = field(default_factory=list)
    veloce: bool = False

    stati: list[str] = field(default_factory=list)  # una voce corta per fonte, per la riga di log finale

    def testo(self) -> str:
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        parti = [f"Research compiled on {oggi}. Each item shows its source and date when known."]
        if self.veloce:
            parti.append("Note: the question closes soon, so this is the FAST research path (web and markets only).")
        parti += self.sezioni
        if self.fonti_fallite:
            parti.append("Sources that failed this time: " + ", ".join(self.fonti_fallite))
        return "\n\n".join(parti)

    def fallita(self, nome: str) -> bool:
        return any(f.startswith(f"{nome} (") for f in self.fonti_fallite)

    def riga_di_log(self, etichetta: str) -> str:
        """Una riga sola, per domanda, con cosa ha lavorato: dal log di GitHub si capisce senza aprire il briefing."""
        via = " (via veloce)" if self.veloce else ""
        riga = f"Fonti per {etichetta}{via}: " + (", ".join(self.stati) or "nessuna")
        if self.fonti_fallite:
            riga += "; fallite: " + ", ".join(self.fonti_fallite)
        return riga


_NOMI_BREVI = {"resolution source": "risoluzione", "prediction markets": "mercati", "data series": "serie"}


def stato_fonte(nome: str, testo: str | None, fallita: bool = False) -> str:
    """Voce corta per la riga di log: "risoluzione 1/2", "mercati 0 abbinati (7 trovati)", "web ok", "serie no"."""
    breve = _NOMI_BREVI.get(nome, nome)
    if fallita:
        return f"{breve} errore"
    if not testo:
        return f"{breve} no"
    if nome == "resolution source":
        letti = testo.count("Fetched:")
        falliti = testo.count("could not be fetched")
        return f"{breve} {letti}/{letti + falliti}" if letti + falliti else f"{breve} ok"
    if nome == "prediction markets":
        trovati = re.search(r"(\d+) candidate markets found", testo)
        if trovati:
            return f"{breve} 0 abbinati ({trovati.group(1)} trovati)"
        abbinati = sum(1 for r in testo.splitlines() if r.startswith("- ["))
        return f"{breve} {abbinati} abbinati"
    return f"{breve} ok"


class Ricercatore:
    def __init__(
        self,
        impostazioni: Impostazioni,
        registro: Registro,
        modello_ricerca: LlmRegistrato,
        modello_riassunto: LlmRegistrato,
        cerca_web=None,
        cerca_notizie=None,
        fonte_risoluzione=None,
        mercati=None,
        serie_storiche=None,
    ) -> None:
        self.imp = impostazioni
        self.registro = registro
        self.modello_ricerca = modello_ricerca
        self.modello_riassunto = modello_riassunto
        # Le fonti sono iniettabili per i test (e per la prova offline).
        self._cerca_web = cerca_web if cerca_web is not None else self._fonte_web_predefinita()
        self._cerca_notizie = cerca_notizie if cerca_notizie is not None else self._fonte_notizie_predefinita()
        self._fonte_risoluzione = fonte_risoluzione if fonte_risoluzione is not None else self._fonte_risoluzione_predefinita()
        self._mercati = mercati if mercati is not None else self._mercati_predefiniti()
        self._serie = serie_storiche if serie_storiche is not None else self._serie_predefinite()

    # ----------------------------------------------------------- fonti
    def _fonte_web_predefinita(self):
        if self.imp.modello_finto:
            return None
        if chiave_presente("ANTHROPIC_API_KEY") or (
            chiave_presente("METACULUS_TOKEN") and self.imp.modello_ricerca.startswith("metaculus/")
        ):
            return self._ricerca_web_claude
        return None

    def _fonte_notizie_predefinita(self):
        if self.imp.modello_finto:
            return None
        # AskNews accetta sia la coppia client id + secret sia la chiave singola ASKNEWS_API_KEY
        # (AskNewsSearcher di forecasting-tools le gestisce entrambe).
        if (chiave_presente("ASKNEWS_CLIENT_ID") and chiave_presente("ASKNEWS_SECRET")) or chiave_presente(
            "ASKNEWS_API_KEY"
        ):
            return self._asknews
        return None

    def _fonte_risoluzione_predefinita(self):
        if self.imp.modello_finto or not self.imp.fonte_risoluzione:
            return None
        from bot.fonti.fonte_risoluzione import fonte_risoluzione

        return fonte_risoluzione

    def _mercati_predefiniti(self):
        if self.imp.modello_finto or not self.imp.mercati:
            return None
        from bot.fonti.mercati import cerca_tutti, evidenza_mercati, piattaforme_attive

        piattaforme = piattaforme_attive(kalshi=self.imp.kalshi)

        async def cerca(query: list[str]):
            return await cerca_tutti(query, piattaforme)

        async def mercati(domanda: MetaculusQuestion) -> str | None:
            return await evidenza_mercati(domanda, self.modello_riassunto, cerca=cerca)

        return mercati

    def _serie_predefinite(self):
        if self.imp.modello_finto or not self.imp.serie_storiche:
            return None
        from bot.fonti.serie_storiche import serie_storiche

        async def serie(domanda: MetaculusQuestion) -> str | None:
            return await serie_storiche(domanda, self.modello_riassunto)

        return serie

    async def _asknews(self, domanda: str) -> str:
        from forecasting_tools import AskNewsSearcher

        usate = self.registro.interroga(
            "SELECT COALESCE(SUM(ricerche_web),0) FROM chiamate WHERE scopo='asknews' AND substr(momento,1,7)=?",
            (datetime.now(timezone.utc).strftime("%Y-%m"),),
        )[0][0]
        if usate >= LIMITE_ASKNEWS_MESE:
            raise RuntimeError("chiamate AskNews del mese esaurite")
        async with _lucchetto_asknews:  # piano gratuito: 1 chiamata ogni 10 secondi
            testo = await AskNewsSearcher().get_formatted_news_async(domanda)
        self.registro.registra_chiamata("asknews/news", 0, 0, costo=0.0, ricerche_web=2, scopo="asknews")
        return testo

    async def _ricerca_web_claude(self, richiesta: str) -> str:
        """Ricerca web lato server di Claude (strumento web_search)."""
        import anthropic

        self.registro.controlla_tetti(domanda_corrente.get())
        modello = self.imp.modello_ricerca.split("/")[-1]
        if self.imp.modello_ricerca.startswith("metaculus/"):
            client = anthropic.AsyncAnthropic(
                api_key=os.environ["METACULUS_TOKEN"],
                base_url="https://llm-proxy.metaculus.com/proxy/anthropic",
                default_headers={"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"},
            )
        else:
            client = anthropic.AsyncAnthropic()
        messaggi: list[dict] = [{"role": "user", "content": richiesta}]
        strumenti = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": self.imp.ricerche_web_per_giro}
        ]
        testi: list[str] = []
        risultati: list[str] = []
        for _ in range(3):  # pause_turn: la ricerca lato server può chiedere di continuare
            risposta = await client.messages.create(
                model=modello,
                max_tokens=8000,
                output_config={"effort": "low"},
                tools=strumenti,
                messages=messaggi,
            )
            uso = risposta.usage
            ricerche = 0
            if getattr(uso, "server_tool_use", None) is not None:
                ricerche = getattr(uso.server_tool_use, "web_search_requests", 0) or 0
            self.registro.registra_chiamata(
                self.imp.modello_ricerca,
                uso.input_tokens + (uso.cache_read_input_tokens or 0) + (uso.cache_creation_input_tokens or 0),
                uso.output_tokens,
                costo=None,
                ricerche_web=ricerche,
                scopo="ricerca_web",
            )
            for blocco in risposta.content:
                if blocco.type == "text":
                    testi.append(blocco.text)
                elif blocco.type == "web_search_tool_result" and isinstance(blocco.content, list):
                    for r in blocco.content:
                        data = getattr(r, "page_age", None) or "data non indicata"
                        risultati.append(f"- {r.title} ({data}) {r.url}")
            if risposta.stop_reason != "pause_turn":
                break
            messaggi = [messaggi[0], {"role": "assistant", "content": risposta.content}]
        if risposta.stop_reason == "refusal":
            raise RuntimeError("la ricerca web è stata rifiutata dal modello")
        corpo = "".join(testi).strip()
        if risultati:
            corpo += "\n\nPages found:\n" + "\n".join(dict.fromkeys(risultati))
        if not corpo:
            raise RuntimeError("ricerca web senza risultati")
        return corpo

    # ------------------------------------------------------------ giri
    def _richiesta_web(self, domanda: MetaculusQuestion, focus: str | None) -> str:
        oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        richiesta = clean_indents(
            f"""
            Today is {oggi}. You are a research assistant for a forecaster. Search the web and report
            the facts most useful for forecasting the question below. For every fact give the source
            and its publication date. Prefer recent, primary and official sources. Include base rates
            (how often similar things happened before), the current status of anything the question
            depends on, and current trends. Do not give a forecast.

            Question: {domanda.question_text}

            Resolution criteria: {domanda.resolution_criteria}

            {domanda.fine_print or ""}
            """
        )
        if focus:
            richiesta += f"\n\nFocus this search on what is still missing:\n{focus}"
        return richiesta

    async def _prova(self, nome: str, coro, esito: EsitoRicerca) -> str | None:
        try:
            testo = await coro
            if testo:
                esito.fonti_usate.append(nome)
            return testo
        except TettoSuperato:
            raise
        except Exception as errore:
            logger.warning(f"Fonte {nome} fallita: {type(errore).__name__}: {errore}")
            esito.fonti_fallite.append(f"{nome} ({type(errore).__name__})")
            return None

    async def _cosa_manca(self, domanda: MetaculusQuestion, note: str) -> str | None:
        prompt = clean_indents(
            f"""
            DOMANDE_MANCANTI
            A forecaster must answer: {domanda.question_text}
            Below are the research notes gathered so far. List at most 3 short web search queries
            for the most important information that is still missing (for example: base rates,
            the latest status, official schedules). One query per line starting with "- ".
            If nothing important is missing, write only: NONE

            Notes:
            {note[:12000]}
            """
        )
        risposta = await self.modello_riassunto.invoke(prompt)
        if "NONE" in risposta.strip().upper()[:10]:
            return None
        righe = [r.strip("-* ").strip() for r in risposta.splitlines() if r.strip().startswith(("-", "*"))]
        return "\n".join(f"- {r}" for r in righe[:3]) or None

    async def ricerca(self, domanda: MetaculusQuestion, veloce: bool = False) -> str:
        esito = EsitoRicerca(veloce=veloce)
        numerica = isinstance(domanda, NumericQuestion)  # le serie storiche servono solo alle numeriche
        compiti: list[tuple[str, object]] = []
        # Le fonti "strutturali" prima: fonte di risoluzione, mercati, serie storiche.
        if self._fonte_risoluzione is not None and not veloce:
            compiti.append(("resolution source", self._fonte_risoluzione(domanda)))
        if self._mercati is not None:
            compiti.append(("prediction markets", self._mercati(domanda)))
        if self._serie is not None and numerica and not veloce:
            compiti.append(("data series", self._serie(domanda)))
        if self._cerca_notizie is not None and not veloce:
            compiti.append(("AskNews", self._cerca_notizie(domanda.question_text)))
        if self._cerca_web is not None:
            compiti.append(("web", self._cerca_web(self._richiesta_web(domanda, None))))
        risultati = await asyncio.gather(*(self._prova(n, c, esito) for n, c in compiti))
        for (nome, _), testo in zip(compiti, risultati):
            esito.stati.append(stato_fonte(nome, testo, fallita=esito.fallita(nome)))
            if testo:
                esito.sezioni.append(f"## Source: {nome} (round 1)\n{testo}")

        if self._cerca_web is not None and esito.sezioni and not veloce:
            for giro in range(2, self.imp.giri_ricerca + 1):
                mancante = await self._prova("analisi lacune", self._cosa_manca(domanda, "\n\n".join(esito.sezioni)), esito)
                if not mancante:
                    break
                nome = f"web giro {giro}"
                testo = await self._prova(nome, self._cerca_web(self._richiesta_web(domanda, mancante)), esito)
                esito.stati.append(stato_fonte(nome, testo, fallita=esito.fallita(nome)))
                if testo:
                    esito.sezioni.append(f"## Source: web (round {giro}, looking for: {mancante})\n{testo}")

        if not esito.sezioni:
            # Ultima risorsa: quello che il modello sa, dichiarato come tale.
            nome = "conoscenza del modello"
            testo = await self._prova(nome, self.modello_ricerca.invoke(self._richiesta_web(domanda, None)), esito)
            esito.stati.append(stato_fonte(nome, testo, fallita=esito.fallita(nome)))
            if testo:
                esito.sezioni.append(
                    "## Source: model background knowledge (no live search available; may be outdated)\n" + testo
                )
        logger.info(esito.riga_di_log(getattr(domanda, "page_url", None) or domanda.question_text[:60]))
        return esito.testo()
