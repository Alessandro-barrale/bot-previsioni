# Resoconto del costruttore – bot v2 (22/10/2026)

Cartella `bot-previsioni/`, 6 commit sopra `b6f034d` (uno per passo più la documentazione), **101 test** verdi con `uv run --no-sync pytest -q` (erano 50). Nessuna chiave nel codice, nei commit o in questo file; `.env` non esiste in locale e non è stato letto. `INVIA_PREVISIONI` non è stata toccata. Nessun `git push`.

## Cosa è fatto, passo per passo

### 1. Prompt v2 (commit `c2f6be9`)
- `bot/previsore.py`: metodo in 7 passi, in quest'ordine: status quo ("se nulla cambiasse, come risolve?", con impegno esplicito), resolution check (criterio già soddisfatto o impossibile => ≥95% / ≤5% e stop), base rate calcolata dai dati con "anchor on your math" (oltre 15 punti serve evidenza nuova e nominata), le tre regole soft clock / history discharged / null result, multi-clausola come prodotto booleano con una probabilità per clausola, evidenza datata, auto-controllo di calibrazione ("X volte su 100", "scenario che renderebbe ridicola la previsione", "90% = 9:1").
- Alternanza **outside view / inside view**: campioni dispari outside, pari inside (3+2 con 5 campioni, 1+1 per modello con 6), stessa ricerca, un'istruzione diversa in testa.
- Numeriche e date: **13 percentili** in un blocco JSON finale obbligatorio, note sul log density score e sulla massa oltre i limiti aperti, larghezza per tipo di serie (amministrata stretta sull'ultimo valore, prezzo volatile random walk sulla varianza storica), date con "aperto = status quo è che non succede nella finestra".
- Blocco JSON finale anche per binarie (`{"probability": 0.37}`) e scelta multipla (`{"option_probabilities": {...}}`). `bot/combina.py` legge l'ultimo oggetto JSON di primo livello (anche dentro i ```), con le regex della v1 come riserva e il parser LLM come ultima riserva.
- Limiti: binario **[0.02, 0.98]**, pavimento scelta multipla **0.01**. Scelta multipla: media con ≥5 campioni, mediana per opzione con meno.
- Verifica della libreria: `NumericReport.aggregate_predictions` di forecasting-tools 0.3.4 fa la **mediana punto per punto delle CDF** su ascisse comuni (poi ri-standardizza): va bene, non sostituita. Il test `test_numeriche_mediana_delle_cdf_non_dei_percentili` lo dimostra con 3 campioni (la media dei percentili darebbe una mediana a circa 44, la mediana delle CDF resta a circa 32). L'interpolazione della libreria è lineare, non PCHIP: con 13 percentili basta, non ho aggiunto scipy.

### 2. Pagella viva (commit `793a509`)
- `bot/pagella.py`: con `METACULUS_TOKEN` scarica i post risolti su cui il bot ha previsto (`/api/posts/?forecaster_id=…&statuses=resolved&with_cp=true`), prende lo spot (ultima nostra previsione prima della chiusura) e la community prediction nello stesso istante, calcola log score nostro e della community (binarie, scelta multipla, numeriche/date dalla massa del bin della CDF in cui cade l'esito, scala lineare o logaritmica, fuori scala compreso) e lo **spot peer score stimato** = 100 × (log nostro − log community). È una stima prudente: la CP batte il previsore medio. Tutto in `dati/pagella.sqlite`, una riga per domanda, aggiornata a ogni giro.
- `--modo pagella` in `main.py`; sezione "Pagella" nel rapporto del lunedì con medie per torneo, quante volte battiamo la community e la tabella delle ultime domande; passo aggiunto a `rapporto.yaml` (commit anche di `dati/pagella.sqlite`).
- L'API di Metaculus rifiuta le chiamate senza token (403), quindi la forma del JSON è quella documentata in forecasting-tools; se un campo differisse, la domanda viene saltata con un avviso nel log e il rapporto va avanti.

### 3. Ricerca v2 (commit `acabce5`)
- `bot/fonti/fonte_risoluzione.py`: URL estratti da criteri, note e sfondo (senza metaculus.com, social, PDF), max 2 pagine, scaricate con timeout 15 s, HTML pulito senza dipendenze, data dalla pagina o dall'header, 6.000 caratteri in tutto, nel briefing come "What the resolution source says NOW".
- `bot/fonti/mercati.py`: Polymarket (Gamma `public-search`), Manifold (`search-markets`), Kalshi (lista dei mercati aperti filtrata per parole chiave: non ha una ricerca testuale pubblica). Abbinamento fatto dal modello economico con JSON `same`/`related`; peso per liquidità 0 a 1k $, 0,04 a 10k, 1 a 100k (lineare in log10); proiezione con hazard costante alla nostra data solo per gli abbinamenti `same`; etichetta weak/strong/negligible.
- `bot/fonti/serie_storiche.py`: il modello economico indica id FRED e ticker Yahoo; FRED solo con `FRED_API_KEY` (mai stampata); Yahoo Finance via l'endpoint `chart` (lo stesso di yfinance, senza aggiungere la dipendenza). Nel briefing: ultimo valore con data, ultimi valori, min/max/mediana, variazioni su finestre lunghe quanto l'orizzonte (media, deviazione standard, percentili 5-95, larghezza random walk).
- `bot/ricerca.py`: le tre fonti nel giro 1 insieme ad AskNews e web; interruttori `FONTE_RISOLUZIONE`, `MERCATI`, `SERIE_STORICHE`; ogni fonte può cadere senza bloccare. AskNews: `AskNewsSearcher` accetta la sola `ASKNEWS_API_KEY` (verificato nel codice della libreria), passata anche nei workflow.
- Tempo: `minuti_alla_chiusura`; sotto 12 minuti `ChiusuraImminente` (registrata, nessuna spesa, il giro non diventa rosso); sotto 40 via veloce (solo web + mercati, niente secondo giro).
- Tetti predefiniti: 0,50 $ medio, **0,90 $** massimo a domanda, **150 $** al mese (config, README, `.env.template`, commenti dei workflow).

### 4. Roster a 3 vendor (commit `0c3b55d`)
- Nomi verificati su `https://openrouter.ai/api/v1/models` il 22/10: `openai/gpt-5.6-sol` (4/20 $ per milione di token), `google/gemini-3.7-flash` (0,75/3,75 $). Esistono anche `openai/gpt-6.1-sol` e `google/gemini-4-argon` (2/10 $, quelli di nostreambot): si scelgono con `MODELLO_OPENAI` / `MODELLO_GOOGLE`.
- Con `OPENROUTER_API_KEY`: Opus 5.5 (Anthropic diretta se c'è la chiave, altrimenti via OpenRouter) + GPT-5.6 Sol + Gemini 3.7 Flash, 6 campioni (2 per vendor, uno outside e uno inside ciascuno), mediana. `ROSTER_3_VENDOR=no` per restare su 3 Opus + 2 Sonnet.
- `reasoning_effort=high` sui modelli OpenAI e Google (`EFFORT_PREVISIONE`); sui modelli Anthropic solo con `EFFORT_ANTHROPIC` (oggi il bot vivo gira senza, e litellm lo tradurrebbe in ragionamento esteso con costo non misurato). `verbosity` non viene mai mandata (rimossa anche se passata). Serve almeno la metà dei campioni: un vendor può cadere.
- Listino dei nuovi modelli in `bot/spese.py` per la stima del costo quando il fornitore non lo dà.

### 5. Bot secondario (commit `2d25be0`)
- `--bot 2`: usa `METACULUS_TOKEN_2`, `dati/registro_v2.sqlite` e `dati/pagella_v2.sqlite`; senza token non parte.
- `.github/workflows/previsioni-v2.yaml`: solo `workflow_dispatch`, nessun cron, `if: vars.BOT_2_ATTIVO == 'si'`, invio con `INVIA_PREVISIONI_2`. **Non attivato.**

## Test

| File | Test | Cosa coprono |
|---|---|---|
| `tests/test_combina.py` | 44 | JSON finale, limiti, 13 percentili, date, regex di riserva, mediana delle CDF contro media dei percentili |
| `tests/test_bot.py` | 23 | bot completo con il modello finto, alternanza outside/inside, prompt numerico, ricerca v2 e guasti, riga di log delle fonti per domanda, via veloce, salto per chiusura, roster 3 vendor, effort senza verbosity, bot secondario, workflow v2 spento |
| `tests/test_fonti.py` | 20 | URL (anche markdown Metaculus con escape e parentesi), pulizia HTML, CSV pubblico di FRED, limiti e guasti della fonte di risoluzione, peso liquidità, hazard, lettura delle tre piattaforme, abbinamento e log dei mercati, serie storiche con chiave FRED, con CSV e con solo Yahoo |
| `tests/test_pagella.py` | 10 | binarie, scelta multipla, numeriche (bin, fuori scala, scala log, date), gruppi, salvataggio, riepilogo, rapporto |
| `tests/test_spese.py` | 10 | listino, tetti, registro, rapporto |
| **Totale** | **107** | tutti offline, nessuna rete |

## Costo per domanda

- **Misurato (v1, registro `dati/registro.sqlite`)**: 2 domande vere, 0,30 e 0,39 $ (media 0,35): previsioni 0,30 $ in 10 chiamate, ricerca web 0,39 $ in 4 chiamate, Haiku meno di 0,01 $.
- **Prova offline v2** (modello finto, token stimati dalla lunghezza dei testi): 0,45 $ a domanda per i 4 tipi, 9 chiamate.
- **Non misurato con chiavi vere**: in locale non c'è `.env`, quindi nessuna prova `test_questions` è stata possibile. La v2 aggiunge 2 chiamate Haiku (abbinamento mercati, scelta della serie: meno di 0,01 $) e testo nel briefing (pagina di risoluzione fino a 6.000 caratteri, mercati, serie: circa 2-3k token in più per campione, cioè +0,02-0,04 $ a domanda con 5 campioni Opus/Sonnet). Stima v2: **0,40-0,50 $** con il roster Claude, fino a 0,55 $ con i 3 vendor. Il primo rapporto del lunedì darà il numero vero.

## Cosa resta

- Prova con chiavi vere di `--modo test_questions` (una sola, sotto i 2 $) per misurare il costo v2 e vedere il briefing con fonte di risoluzione, mercati e serie su domande reali.
- Polymarket e Kalshi: da questa macchina le due API danno errore TLS (probabile blocco geografico o proxy); il codice segue la documentazione ed è coperto dai test con risposte finte. Da GitHub Actions (runner USA) dovrebbero rispondere; se no, la fonte cade in modo pulito (riga "Sources that failed").
- La forma del JSON dell'API Metaculus per la pagella (`my_forecasts.history`, `aggregations.recency_weighted.history`) va confermata al primo giro con il token: il log dice quali domande non sono valutabili.
- Interpolazione PCHIP ed effort alto su Anthropic: lasciati fuori (il primo non serve con 13 percentili, il secondo costa in modo non misurato).
- Market Pulse e aggiornamenti continui: fatti dopo, vedi la sezione "Market Pulse 26Q4" in fondo.

## Cosa serve dall'utente

1. **Niente per la v2 base**: con i segreti attuali (`METACULUS_TOKEN`, `ANTHROPIC_API_KEY`) il prossimo `git push` accende prompt v2, ricerca v2 e pagella.
2. Facoltativi, tutti segreti GitHub: `OPENROUTER_API_KEY` (roster a 3 vendor), `ASKNEWS_API_KEY` (notizie), `FRED_API_KEY` (serie ufficiali), `METACULUS_TOKEN_2` (secondo bot, poi variabile `BOT_2_ATTIVO=si` e una sveglia per farlo girare).
3. Decidere il push: tutto è in commit locali, non è stato pubblicato niente.

---

# Market Pulse 26Q4 (aggiunta del 22/10/2026)

Commit `558e35d` (registro per torneo e fonte dei risultati trimestrali) e `016fca9` (modo `market_pulse`, workflow, rapporto), più quello di questa documentazione. **155 test** verdi con `uv run --no-sync pytest -q` (erano 118: 13 sui trimestrali, 24 su Market Pulse), tutti offline; ogni commit è stato provato da solo in un worktree pulito. Nessuna chiave nel codice, nei log o nei commit; `.env` non letto; variabili del repo non toccate; nessun push. Il passo resta spento finché la direzione non mette `MARKET_PULSE_ATTIVO` = `si`.

## Cosa è fatto

- **Domande di gruppo.** forecasting-tools 0.3.4 le espone già come `NumericQuestion` singole: `get_all_open_questions_from_tournament` usa `group_question_mode="unpack_subquestions"`, filtra lo stato sotto-domanda per sotto-domanda e mette l'etichetta in `group_question_option`; `CURRENT_MARKET_PULSE_ID` è `market-pulse-26q4`. Però scarta il gruppo intero se manca `fine_print`, `description` o `resolution_criteria` (KeyError), sostituisce i criteri della sotto-domanda con quelli del gruppo e aspetta 3,5-4,5 s a richiesta. Quindi `bot/market_pulse.py` legge prima i post direttamente (una richiesta) con uno spacchettatore tollerante che usa lo stesso `DataOrganizer` della libreria, e chiama la libreria solo se la lettura diretta fallisce o non capisce una sotto-domanda; unione per chiave. Ogni sotto-domanda va nella pipeline v2 (ricerca, 5-6 campioni, mediana delle CDF).
- **Calendario a costo controllato.** Scadenza = il primo tra `spot_scoring_time` e la chiusura. Prima previsione quando la sotto-domanda compare; aggiornamento solo nelle ultime 6 ore prima della scadenza e solo se la prima ha almeno 1 giorno; poi niente. Al massimo 12 sotto-domande per giro, prima gli aggiornamenti e poi la scadenza più vicina. Le previsioni fatte si leggono dal registro filtrato per torneo e, quando si invia, dalla storia `my_forecasts` dell'API (se un registro non fosse stato salvato non si rifà la prima). La libreria non salta le domande già previste (`skip_previously_forecasted_questions=False`): decide il calendario.
- **Registro.** Lo stesso `dati/registro.sqlite`. Colonna `torneo` nelle chiamate (aggiunta da sola ai registri vecchi: provato su una copia del registro vero, 43 chiamate e 2,27 $ invariati). Solo `BotMarketPulse` ammette due previsioni per domanda; `BotPrevisioni` degli altri tornei salta ancora la domanda già prevista (test `test_doppia_previsione_solo_in_market_pulse`).
- **Tetti.** `TETTO_MESE_MP_USD` 30 $ e `TETTO_GIORNO_MP_USD` 8 $ (questo l'ho aggiunto io: senza, la prima accensione con decine di sotto-domande aperte potrebbe consumare il tetto giornaliero generale di 15 $ e fermare FutureEval), contati filtrando per torneo e dentro i tetti generali. Tetto per domanda per passata, 2 × `TETTO_DOMANDA_USD` sul totale. Vicino a un tetto proprio le prime previsioni aspettano e restano gli aggiornamenti.
- **Ricerca.** `bot/fonti/trimestrali.py` al posto delle serie generiche: ricavi, EPS diluito, utile netto degli ultimi trimestri dalla SEC (XBRL, quarto trimestre ricavato dall'anno), consenso del trimestre in corso e del prossimo, sorprese EPS e data dei risultati da Yahoo, prezzo come contesto. Provato dal vivo da qui: Yahoo e SEC rispondono per NVDA, AAPL, TSLA (consenso NVDA trimestre al 31/10/2026: 108,98 B $ di ricavi, 45 analisti; 4 beat EPS su 4). La SEC rifiuta con 403 lo User-Agent con l'indirizzo noreply di GitHub: il predefinito usa `example.com` (dominio riservato) e si cambia con `SEC_USER_AGENT`. La guida del primo giro web chiede consenso, guidance, data e beat/miss degli ultimi 8 trimestri (la storia delle sorprese sui ricavi non è nelle fonti gratuite).
- **Prompt.** Nota per le numeriche di Market Pulse: serie amministrata, centro sul consenso o sulla guidance più recente spostato della sorpresa tipica, larghezza dalla storia dei beat/miss e non dalla volatilità del titolo, massa oltre i limiti aperti, ora dello spot scoring; all'aggiornamento il prompt lo dice. Gli altri tornei hanno il prompt identico a prima.
- **Main e workflow.** `--modo market_pulse`; con `--bot 2` esce con errore (si entra una volta sola, e mai con l'account umano). Passo nel job `previsioni` dopo il torneo principale, `if: vars.MARKET_PULSE_ATTIVO == 'si'`, `continue-on-error: true`, `timeout-minutes: 40`, token `secrets.METACULUS_TOKEN`; il registro lo salva il passo finale già esistente.
- **Rapporto.** Spesa del mese di Market Pulse e classifica del torneo, solo quando ci sono previsioni.

## Non verificabile senza token

La forma esatta del JSON dei gruppi di Market Pulse (l'API risponde 403 senza token). Il codice regge le due forme plausibili, con test per entrambe: (A) testi a livello di gruppo, `status` e `spot_scoring_time` su ogni sotto-domanda (la libreria e la lettura diretta danno le stesse domande); (B) testi solo sulla sotto-domanda, niente `status` (dedotto da apertura e chiusura), niente `spot_scoring_time` (si usa la chiusura), niente `default_project` (la libreria scarta il gruppo, la lettura diretta no); più i post non di gruppo.

## Cosa guardare al primo giro vero su GitHub

1. La riga `Market Pulse market-pulse-26q4: N sotto-domande aperte in M post (lettura diretta)`: N deve essere vicino alle sotto-domande aperte sul sito. Se compare `libreria chiamata perché: ...`, il motivo dice cosa non è stato capito.
2. La riga `Market Pulse: N sotto-domande aperte; in questo giro X (a prime, b aggiornamenti) [motivi]`: se i motivi dicono `nessuno spot scoring noto`, il campo `spot_scoring_time` non c'è e la scadenza è la chiusura (va bene, ma da sapere).
3. `Trimestrali per NVDA: SEC, Yahoo consenso, Yahoo prezzo`: dal runner Yahoo potrebbe rispondere 429 o negare il crumb; la fonte cade pulita e resta il web.
4. Sul sito, che le previsioni stiano sulle sotto-domande giuste dei gruppi (l'invio usa `id_of_question`).
5. La durata del passo e del job: senza sotto-domande da fare il passo dura pochi secondi, ma va visto se il job passa al minuto successivo.
6. Nel rapporto del lunedì la riga `Market Pulse, mese ...` e i costi a passata (stima 0,5 $).

## Cosa serve dalla direzione

1. Accendere: variabile `MARKET_PULSE_ATTIVO` = `si` (con `INVIA_PREVISIONI` = `si` invia davvero).
2. Facoltativo: alzare `TETTO_MESE_USD` di 30 $ (oggi 100 $ nel repo, secondo il commento del workflow), perché la spesa di Market Pulse conta anche nel tetto generale e altrimenti la toglie al torneo principale.
3. Facoltativo: `SEC_USER_AGENT` con un contatto vero ("nome indirizzo"), come chiede la SEC.
4. Il push.
