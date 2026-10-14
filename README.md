# Bot di previsioni per Metaculus FutureEval

Bot automatico per il torneo per bot **FutureEval Fall 2026** (50.000 $) e per i **MiniBench** ogni due settimane (1.000 $).
Parte dal [template ufficiale](https://github.com/Metaculus/metac-bot-template) (commit `da5de87`, classe `FallTemplateBot2026` in `template_bot.py`) e lo estende.

Le regole vietano di intervenire sulle previsioni: dopo l'attivazione il bot lavora da solo, e tu guardi solo il rapporto del lunedì in `rapporti/`.

## Cosa fa a ogni giro (ogni ora)

1. Legge le domande aperte del torneo stagionale e del MiniBench e salta quelle già previste.
2. **Ricerca** a più fonti e in più giri:
   - AskNews (notizie recenti e archivio) e ricerca web di Claude, in parallelo;
   - poi un modello economico elenca cosa manca, e si fa un secondo giro di ricerca web;
   - ogni fonte riporta la data, e se una fonte cade si va avanti con le altre.
3. **5 previsioni indipendenti**, a rotazione su due modelli (3 con Opus 5.5 e 2 con Sonnet 5.5). Il prompt chiede il metodo del previsore: frequenza di base, tempo rimasto, status quo, scenari.
4. **Combinazione:**
   - domande sì/no: la mediana, sempre tra 1% e 99%;
   - scelte multiple: la media, con un minimo su ogni opzione;
   - domande numeriche e date: percentili ordinati e strettamente crescenti dentro i limiti, poi la mediana delle CDF.
5. **Registro** SQLite (`dati/registro.sqlite`) di ogni chiamata (modello, token, costo) e di ogni previsione, salvato nel repository a fine giro.
6. **Tetti di spesa** controllati prima di ogni chiamata: per domanda, per giorno e per mese. Quando si raggiunge un tetto la domanda viene saltata: vale zero punti, ma non si spende.

## I tuoi passi (una volta sola, circa 45 minuti)

| # | Cosa | Dove | Tempo |
|---|---|---|---|
| 1 | Crea l'account Metaculus, poi il bot dalla pagina *Participate* e copia il suo token | https://www.metaculus.com/futureeval/participate/ | 10 min |
| 2 | Compila il modulo di partecipazione e chiedi i crediti gratuiti per i modelli | link nella pagina Participate | 10 min |
| 3 | (Facoltativo) Crea l'account AskNews e prendi *client id* e *secret* | https://asknews.app/ | 5 min |
| 4 | Prendi una chiave API dalla Console Anthropic | https://console.anthropic.com/ | 5 min |
| 5 | Crea un repository **privato** su GitHub e caricaci questa cartella (senza `.env` e senza `.venv`, già esclusi) | github.com → New repository | 5 min |
| 6 | Inserisci i segreti e le variabili (tabella sotto) | Settings → Secrets and variables → Actions | 5 min |
| 7 | Lancia a mano **Prova** con "domande vere" spuntato e guarda che finisca in verde | Actions → Prova → Run workflow | 5 min |
| 8 | Attiva l'invio: variabile `INVIA_PREVISIONI` = `si` | Settings → Variables | 1 min |

Da quel momento non toccare più niente: il workflow **Previsioni** gira ogni ora e il **Rapporto** ogni lunedì.

### Segreti (Settings → Secrets and variables → Actions → *Secrets*)

| Nome esatto | Obbligatorio | Cosa |
|---|---|---|
| `METACULUS_TOKEN` | sì | token del bot Metaculus |
| `ANTHROPIC_API_KEY` | consigliato | chiave della Console Anthropic: modelli Claude e ricerca web |
| `OPENROUTER_API_KEY` | no | chiave OpenRouter dei crediti Metaculus (usata se manca la chiave Anthropic) |
| `ASKNEWS_CLIENT_ID` | no | AskNews |
| `ASKNEWS_SECRET` | no | AskNews |

Senza chiavi dei modelli il bot usa il proxy di Metaculus con il solo `METACULUS_TOKEN`. In quel caso la ricerca web resta un tentativo: se il proxy non la supporta, il bot usa le altre fonti.

### Variabili (stessa pagina, scheda *Variables*)

| Nome esatto | Predefinito | Cosa |
|---|---|---|
| `INVIA_PREVISIONI` | (vuoto = no) | `si` per inviare davvero le previsioni a Metaculus |
| `TETTO_DOMANDA_USD` | `0.80` | spesa massima per una domanda |
| `TETTO_GIORNO_USD` | `15` | spesa massima al giorno |
| `TETTO_MESE_USD` | `120` | spesa massima al mese |
| `MODELLI_PREVISIONE` | Opus 5.5 + Sonnet 5.5 | lista separata da virgole in formato litellm, per esempio `anthropic/claude-opus-5-5,openrouter/openai/<modello>` per aggiungere un terzo fornitore con i crediti OpenRouter (copia il nome esatto da openrouter.ai/models) |

## Prove in locale

Serve [uv](https://docs.astral.sh/uv/). Dalla cartella del bot:

```bash
uv sync                                                   # installa tutto in .venv
uv run pytest -q                                          # test
MODELLO_FINTO=si uv run python main.py --modo prova_offline           # 4 domande finte, nessuna rete
MODELLO_FINTO=si uv run python main.py --modo prova_offline --guasto  # una fonte di ricerca che non risponde
```

In PowerShell scrivi `$env:MODELLO_FINTO='si'` su una riga, e su quella dopo il comando.

Con le chiavi vere in `.env` (copia `.env.template`), questo comando prova le domande dell'area di prova di Metaculus **senza inviare niente**, perché `INVIA_PREVISIONI=no`:

```bash
uv run python main.py --modo test_questions
```

Altri modi: `--modo torneo` (stagionale + MiniBench), `--modo stagione`, `--modo minibench`, `--modo rapporto`.

## Costi

Prezzi per milione di token: Opus 5.5 4 $ in ingresso e 20 $ in uscita, Sonnet 5.5 2/10 $, Haiku 5.5 0,10/0,50 $. La ricerca web costa 10 $ ogni 1.000 ricerche.

| Voce per domanda | Stima |
|---|---|
| 3 previsioni Opus + 2 Sonnet (circa 8.500 token letti e 1.800 scritti ciascuna) | 0,28 $ |
| Ricerca web, 2 giri da 3 ricerche | 0,15 $ |
| Analisi di cosa manca (Haiku) | < 0,01 $ |
| AskNews | 0 $ (piano gratuito) |
| **Totale** | **circa 0,43 $** (0,36 $ quando il secondo giro non serve) |

Al mese, con circa 250 domande (MiniBench più torneo stagionale), fanno **circa 90-110 $**. Il tetto mensile di 120 $ blocca comunque la spesa. I numeri vengono dalla prova con il modello finto: i token sono stimati dalla lunghezza dei testi veri. Il costo reale per domanda compare nel registro e nel rapporto dopo i primi giri.

## Minuti di GitHub Actions

Un repository privato ha 2.000 minuti gratis al mese, e GitHub arrotonda ogni giro al minuto intero.

- **Giro senza domande nuove:**
  - misurato in locale: l'avvio del bot e il controllo delle domande durano circa 3 secondi, più le due chiamate alla lista domande di Metaculus;
  - su GitHub, con le dipendenze in cache, a questo si aggiungono checkout, uv e installazione: in tutto circa 30-40 secondi;
  - quindi ogni giro conta **1 minuto**.
- **Frequenza scelta:** ogni ora, cioè 24 × 30 = **720 minuti** al mese.
- **Domande vere:** circa 250 al mese, 3 in parallelo, circa 1-1,5 minuti l'una, cioè **250-400 minuti**.
- **Rapporto settimanale:** circa **5 minuti** al mese.
- **Totale:** circa **1.000-1.150 minuti** su 2.000.

Ogni 30 minuti si arriverebbe a circa 1.850 minuti, troppo vicino al limite. Il template ufficiale gira ogni 20 minuti: con un repository privato non ci si sta, con uno pubblico sì, perché lì i minuti sono illimitati, ma il codice diventa visibile a tutti.

## File

| File | Cosa |
|---|---|
| `main.py` | avvio e modi |
| `template_bot.py` | template ufficiale Metaculus, non modificato |
| `bot/previsore.py` | il bot (`BotPrevisioni`, estende `FallTemplateBot2026`) |
| `bot/ricerca.py` | ricerca a più fonti e a più giri |
| `bot/combina.py` | lettura delle risposte e combinazione delle previsioni |
| `bot/spese.py` | registro SQLite e tetti di spesa |
| `bot/llm.py` | modelli con registro, e il modello finto per le prove |
| `bot/rapporto.py` | rapporto settimanale |
| `.github/workflows/` | Previsioni (ogni ora), Rapporto (lunedì), Prova (a mano) |
| `upstream/` | file originali del template, per confronto |
