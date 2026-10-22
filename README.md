# Bot di previsioni per Metaculus FutureEval (v2)

Bot automatico per il torneo per bot **FutureEval Fall 2026** (50.000 $) e per i **MiniBench** ogni due settimane (1.000 $).
Parte dal [template ufficiale](https://github.com/Metaculus/metac-bot-template) (commit `da5de87`, classe `FallTemplateBot2026` in `template_bot.py`) e lo estende.

Le regole vietano di intervenire sulle previsioni: dopo l'attivazione il bot lavora da solo, e tu guardi solo il rapporto del lunedì in `rapporti/`.

## Cosa fa a ogni giro

1. Legge le domande aperte del torneo stagionale e del MiniBench e salta quelle già previste.
2. **Tempo:** le domande restano aperte 3 ore. Se mancano meno di 12 minuti alla chiusura la domanda viene saltata (una previsione non pubblicata vale zero); sotto i 40 minuti si fa la **via veloce** (solo ricerca web e mercati, un giro).
3. **Ricerca v2**, tutto in parallelo e con ogni fonte che può cadere senza bloccare le altre:
   - la **fonte di risoluzione**: le pagine citate nei criteri e nelle note vengono scaricate (max 2 pagine, 6.000 caratteri), con la data, e messe nel briefing come "cosa dice ORA la fonte";
   - i **mercati di previsione** (Polymarket, Manifold, Kalshi, API pubbliche gratuite): un modello economico decide quali mercati sono davvero sulla stessa domanda, poi il prezzo entra come evidenza pesata per liquidità (0 a 1.000 $ di volume, 4% a 10.000 $, pieno verso 100.000 $) e proiettato alla nostra data;
   - le **serie storiche** per le domande numeriche: FRED (API con `FRED_API_KEY`, altrimenti il CSV pubblico senza chiave) e Yahoo Finance; ultimi valori e variabilità su finestre lunghe quanto il tempo che resta;
   - **AskNews** (con la chiave) e la **ricerca web di Claude**; poi un modello economico elenca cosa manca e si fa un secondo giro web.
4. **Previsioni indipendenti** con il **prompt v2** (metodo dei bot vincenti): status quo e resolution check prima di tutto, base rate calcolata dai dati e "anchor on your math", regole soft clock / history discharged / null result, domande multi-clausola come prodotto booleano, auto-controllo di calibrazione. Metà dei campioni ragiona dall'**outside view** (classe di riferimento), metà dall'**inside view** (meccanismi), sulla stessa ricerca. Ogni risposta chiude con un blocco JSON obbligatorio (le espressioni regolari restano come riserva).
   - senza OpenRouter: 5 campioni, 3 con Opus 5.5 e 2 con Sonnet 5.5;
   - con `OPENROUTER_API_KEY`: **3 vendor**, Opus 5.5 + GPT-5.6 Sol + Gemini 3.7 Flash, 2 campioni ciascuno, effort alto sui modelli OpenAI e Google.
5. **Combinazione:**
   - domande sì/no: la mediana, sempre tra 2% e 98%;
   - scelte multiple: la media con 5 o più campioni, la mediana per opzione con meno, pavimento 1% su ogni opzione;
   - domande numeriche e date: 13 percentili (1, 2.5, 5, 10, 20, 40, 50, 60, 80, 90, 95, 97.5, 99) puliti e strettamente crescenti, poi la **mediana punto per punto delle CDF** (mai la media dei percentili: un test lo garantisce).
6. **Registro** SQLite (`dati/registro.sqlite`) di ogni chiamata (modello, token, costo) e di ogni previsione, salvato nel repository a fine giro.
7. **Tetti di spesa** controllati prima di ogni chiamata: per domanda, per giorno e per mese. Quando si raggiunge un tetto la domanda viene saltata: vale zero punti, ma non si spende.

Il lunedì il **rapporto** aggiunge la **pagella viva**: per ogni domanda risolta confronta la nostra ultima previsione con la community prediction al momento della chiusura (log score nostro contro log score della community, spot peer score stimato), con le medie per torneo. I dati stanno in `dati/pagella.sqlite`.

## I tuoi passi (una volta sola, circa 50 minuti)

| # | Cosa | Dove | Tempo |
|---|---|---|---|
| 1 | Crea l'account Metaculus, poi il bot dalla pagina *Participate* e copia il suo token | https://www.metaculus.com/futureeval/participate/ | 10 min |
| 2 | Compila il modulo di partecipazione e chiedi i crediti gratuiti per i modelli | link nella pagina Participate | 10 min |
| 3 | (Facoltativo) Crea l'account AskNews e prendi la chiave `ASKNEWS_API_KEY` | https://asknews.app/ | 5 min |
| 4 | Collega un'organizzazione della Console all'abbonamento Max: arrivano 200 $ al mese di crediti API. Ogni abbonamento si collega a una sola organizzazione e dopo non puoi cambiarla da solo, quindi scegli quella che userai per tutti i progetti | claude.ai → Impostazioni → Fatturazione → Crediti API | 5 min |
| 5 | Nella Console crea uno spazio di lavoro "bot-previsioni" con un tetto di spesa di 150 $ al mese, e lì crea la chiave API | https://platform.claude.com/ → Workspaces | 5 min |
| 6 | Crea un repository **privato** su GitHub e caricaci questa cartella (senza `.env` e senza `.venv`, già esclusi) | github.com → New repository | 5 min |
| 7 | Inserisci i segreti e le variabili (tabella sotto) | Settings → Secrets and variables → Actions | 5 min |
| 8 | Lancia a mano **Prova** con "domande vere" spuntato e guarda che finisca in verde. Se tra le domande c'è una discreta (numeri interi), controlla che abbia la sua previsione | Actions → Prova → Run workflow | 5 min |
| 9 | Attiva l'invio: variabile `INVIA_PREVISIONI` = `si` | Settings → Variables | 1 min |

Da quel momento non toccare più niente: il workflow **Previsioni** gira (vedi `SVEGLIA.md` per chi lo sveglia) e il **Rapporto** ogni lunedì.

### Segreti (Settings → Secrets and variables → Actions → *Secrets*)

| Nome esatto | Obbligatorio | Cosa |
|---|---|---|
| `METACULUS_TOKEN` | sì | token del bot Metaculus |
| `ANTHROPIC_API_KEY` | consigliato | chiave dello spazio di lavoro "bot-previsioni" (passo 5): modelli Claude e ricerca web |
| `OPENROUTER_API_KEY` | no | chiave OpenRouter (crediti Metaculus o tua): accende il roster a 3 vendor (GPT-5.6 Sol e Gemini 3.7 Flash accanto a Opus 5.5); se manca la chiave Anthropic, anche i modelli Claude passano da qui |
| `ASKNEWS_API_KEY` | no | AskNews con la chiave singola (va bene anche la coppia `ASKNEWS_CLIENT_ID` + `ASKNEWS_SECRET`) |
| `FRED_API_KEY` | no | serie storiche FRED per le domande numeriche (gratis: https://fred.stlouisfed.org/docs/api/api_key.html); senza, si legge il CSV pubblico di FRED (stessi dati, nessuna chiave) |
| `METACULUS_TOKEN_2` | no | token del **bot secondario** (per esempio `antivex-bot-2`) per i test A/B; vedi sotto |

Senza chiavi dei modelli il bot usa il proxy di Metaculus con il solo `METACULUS_TOKEN`. In quel caso la ricerca web resta un tentativo: se il proxy non la supporta, il bot usa le altre fonti.

### Variabili (stessa pagina, scheda *Variables*)

| Nome esatto | Predefinito | Cosa |
|---|---|---|
| `INVIA_PREVISIONI` | (vuoto = no) | `si` per inviare davvero le previsioni a Metaculus |
| `TETTO_DOMANDA_USD` | `0.90` | spesa massima per una domanda (obiettivo medio 0,50 $) |
| `TETTO_GIORNO_USD` | `15` | spesa massima al giorno |
| `TETTO_MESE_USD` | `150` | spesa massima al mese |
| `MODELLI_PREVISIONE` | vedi sopra | lista separata da virgole in formato litellm; sostituisce il roster predefinito |
| `ROSTER_3_VENDOR` | `si` | `no` per restare su 3 Opus + 2 Sonnet anche con la chiave OpenRouter |
| `MODELLO_OPENAI`, `MODELLO_GOOGLE` | `openai/gpt-5.6-sol`, `google/gemini-3.7-flash` | i due modelli del roster via OpenRouter (nomi da openrouter.ai/models, per esempio `openai/gpt-6.1-sol`, `google/gemini-4-argon`) |
| `EFFORT_PREVISIONE` | `high` | `reasoning_effort` sui modelli OpenAI e Google (`no` per toglierlo) |
| `EFFORT_ANTHROPIC` | (vuoto) | `high` per chiedere il ragionamento esteso anche a Opus/Sonnet (costo non ancora misurato) |
| `FONTE_RISOLUZIONE`, `MERCATI`, `SERIE_STORICHE` | `si` | interruttori delle fonti v2: `no` per spegnerne una |
| `MINUTI_SALTO`, `MINUTI_VIA_VELOCE` | `12`, `40` | soglie di tempo alla chiusura |
| `BOT_2_ATTIVO`, `INVIA_PREVISIONI_2`, `MODELLI_PREVISIONE_2` | (vuoto) | bot secondario: `si` per abilitare il workflow *Previsioni v2*, `si` per farlo inviare, roster suo |

## Bot secondario (test A/B)

Metaculus ammette bot secondari (non premiati, nascosti). Con il segreto `METACULUS_TOKEN_2` e `--bot 2` il bot usa quel token e registri separati (`dati/registro_v2.sqlite`, `dati/pagella_v2.sqlite`). Il workflow `previsioni-v2.yaml` è **spento**: non ha cron e parte solo a mano, e solo se la variabile `BOT_2_ATTIVO` vale `si`. Per farlo girare da solo per un MiniBench intero serve aggiungergli una sveglia (come in `SVEGLIA.md`); si promuove a primario solo se la sua pagella è migliore.

## Prove in locale

Serve [uv](https://docs.astral.sh/uv/). Dalla cartella del bot:

```bash
uv sync                                                   # installa tutto in .venv
uv run pytest -q                                          # 101 test, nessuna rete
MODELLO_FINTO=si uv run python main.py --modo prova_offline           # 4 domande finte, nessuna rete
MODELLO_FINTO=si uv run python main.py --modo prova_offline --guasto  # una fonte di ricerca che non risponde
```

In PowerShell scrivi `$env:MODELLO_FINTO='si'` su una riga, e su quella dopo il comando.

Con le chiavi vere in `.env` (copia `.env.template`), questo comando prova le domande dell'area di prova di Metaculus **senza inviare niente**, perché `INVIA_PREVISIONI=no`:

```bash
uv run python main.py --modo test_questions
uv run python main.py --modo pagella          # scarica le domande risolte e stampa la pagella
uv run python main.py --modo rapporto         # rapporto settimanale in rapporti/
```

Altri modi: `--modo torneo` (stagionale + MiniBench), `--modo stagione`, `--modo minibench`; `--bot 2` per il bot secondario.

## Costi

Prezzi per milione di token: Opus 5.5 4 $ in ingresso e 20 $ in uscita, Sonnet 5.5 2/10 $, Haiku 5.5 0,10/0,50 $, GPT-5.6 Sol 4/20 $, Gemini 3.7 Flash 0,75/3,75 $. La ricerca web costa 10 $ ogni 1.000 ricerche.

| Voce per domanda | Stima |
|---|---|
| Previsioni: 3 Opus + 2 Sonnet (v1 misurata: 0,30 $ in 10 chiamate su 2 domande) oppure 2 Opus + 2 GPT-5.6 + 2 Gemini | 0,30-0,38 $ |
| Ricerca web, 2 giri da 3 ricerche (v1 misurata: 0,10 $ a giro) | 0,15-0,20 $ |
| Abbinamento mercati, scelta della serie, analisi di cosa manca (Haiku) | < 0,01 $ |
| Fonte di risoluzione, mercati, serie storiche, AskNews | 0 $ |
| **Totale** | **circa 0,45-0,55 $** (le prime 2 domande vere della v1 sono costate 0,30 e 0,39 $) |

Al mese, con circa 250 domande, fanno **circa 110-140 $**. Il tetto mensile di 150 $ blocca comunque la spesa, e quello per domanda (0,90 $) ferma le domande che sforano.

Questa spesa la coprono i crediti API inclusi in Max 20x: 200 $ al mese, che scadono a fine ciclo ([documentazione ufficiale](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)). Il costo reale per domanda compare nel registro e nel rapporto.

## Minuti di GitHub Actions

Vedi `SVEGLIA.md`: la sveglia principale è esterna (cron-job.org ogni 30 minuti), il cron di GitHub è solo una riserva ogni 6 ore. Totale previsto circa 1.850 minuti al mese sui 2.000 gratuiti di un repository privato. Il bot secondario, quando acceso, conta a parte.

## File

| File | Cosa |
|---|---|
| `main.py` | avvio e modi (`--modo`, `--bot`) |
| `template_bot.py` | template ufficiale Metaculus, non modificato |
| `bot/previsore.py` | il bot (`BotPrevisioni`, estende `FallTemplateBot2026`): prompt v2, angoli outside/inside, tempo alla chiusura |
| `bot/ricerca.py` | ricerca a più fonti e a più giri, via veloce |
| `bot/fonti/` | fonte di risoluzione, mercati di previsione, serie storiche |
| `bot/combina.py` | lettura del JSON finale (regex di riserva) e combinazione delle previsioni |
| `bot/pagella.py` | pagella viva: noi contro la community sulle domande risolte |
| `bot/spese.py` | registro SQLite e tetti di spesa |
| `bot/llm.py` | modelli con registro ed effort per fornitore, e il modello finto per le prove |
| `bot/config.py` | impostazioni dalle variabili d'ambiente, roster dei modelli |
| `bot/rapporto.py` | rapporto settimanale (domande, spesa, pagella, classifica) |
| `.github/workflows/` | Previsioni, Rapporto (lunedì, con la pagella), Prova (a mano), Previsioni v2 (bot secondario, spento) |
| `upstream/` | file originali del template, per confronto |
