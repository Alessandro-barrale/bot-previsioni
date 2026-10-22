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
| `tests/test_bot.py` | 22 | bot completo con il modello finto, alternanza outside/inside, prompt numerico, ricerca v2 e guasti, via veloce, salto per chiusura, roster 3 vendor, effort senza verbosity, bot secondario, workflow v2 spento |
| `tests/test_fonti.py` | 15 | URL, pulizia HTML, limiti e guasti della fonte di risoluzione, peso liquidità, hazard, lettura delle tre piattaforme, abbinamento, serie storiche con e senza FRED |
| `tests/test_pagella.py` | 10 | binarie, scelta multipla, numeriche (bin, fuori scala, scala log, date), gruppi, salvataggio, riepilogo, rapporto |
| `tests/test_spese.py` | 10 | listino, tetti, registro, rapporto |
| **Totale** | **101** | tutti offline, nessuna rete |

## Costo per domanda

- **Misurato (v1, registro `dati/registro.sqlite`)**: 2 domande vere, 0,30 e 0,39 $ (media 0,35): previsioni 0,30 $ in 10 chiamate, ricerca web 0,39 $ in 4 chiamate, Haiku meno di 0,01 $.
- **Prova offline v2** (modello finto, token stimati dalla lunghezza dei testi): 0,45 $ a domanda per i 4 tipi, 9 chiamate.
- **Non misurato con chiavi vere**: in locale non c'è `.env`, quindi nessuna prova `test_questions` è stata possibile. La v2 aggiunge 2 chiamate Haiku (abbinamento mercati, scelta della serie: meno di 0,01 $) e testo nel briefing (pagina di risoluzione fino a 6.000 caratteri, mercati, serie: circa 2-3k token in più per campione, cioè +0,02-0,04 $ a domanda con 5 campioni Opus/Sonnet). Stima v2: **0,40-0,50 $** con il roster Claude, fino a 0,55 $ con i 3 vendor. Il primo rapporto del lunedì darà il numero vero.

## Cosa resta

- Prova con chiavi vere di `--modo test_questions` (una sola, sotto i 2 $) per misurare il costo v2 e vedere il briefing con fonte di risoluzione, mercati e serie su domande reali.
- Polymarket e Kalshi: da questa macchina le due API danno errore TLS (probabile blocco geografico o proxy); il codice segue la documentazione ed è coperto dai test con risposte finte. Da GitHub Actions (runner USA) dovrebbero rispondere; se no, la fonte cade in modo pulito (riga "Sources that failed").
- La forma del JSON dell'API Metaculus per la pagella (`my_forecasts.history`, `aggregations.recency_weighted.history`) va confermata al primo giro con il token: il log dice quali domande non sono valutabili.
- Interpolazione PCHIP ed effort alto su Anthropic: lasciati fuori (il primo non serve con 13 percentili, il secondo costa in modo non misurato).
- Market Pulse e aggiornamenti continui: fuori dal piano v2.

## Cosa serve dall'utente

1. **Niente per la v2 base**: con i segreti attuali (`METACULUS_TOKEN`, `ANTHROPIC_API_KEY`) il prossimo `git push` accende prompt v2, ricerca v2 e pagella.
2. Facoltativi, tutti segreti GitHub: `OPENROUTER_API_KEY` (roster a 3 vendor), `ASKNEWS_API_KEY` (notizie), `FRED_API_KEY` (serie ufficiali), `METACULUS_TOKEN_2` (secondo bot, poi variabile `BOT_2_ATTIVO=si` e una sveglia per farlo girare).
3. Decidere il push: tutto è in commit locali, non è stato pubblicato niente.
