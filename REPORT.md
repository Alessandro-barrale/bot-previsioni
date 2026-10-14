# Resoconto del costruttore – bot di previsioni Metaculus (14/10/2026)

## Cosa c'è e come si avvia

C'è un bot completo in `bot-previsioni/`. Parte dal template ufficiale (classe `FallTemplateBot2026`, commit `da5de87`, forecasting-tools 0.3.4) e aggiunge:

- ricerca su più fonti e in più giri;
- 5 previsioni per domanda su due modelli;
- la combinazione delle previsioni;
- i tetti di spesa e il registro SQLite;
- il rapporto settimanale in italiano;
- 3 workflow GitHub Actions.

I dettagli sono nel `README.md`.

- **Prova senza chiavi:** `MODELLO_FINTO=si uv run python main.py --modo prova_offline`
- **Prova con chiavi, senza invio:** `uv run python main.py --modo test_questions`
- **Su GitHub:**
  - workflow *Previsioni* ogni ora;
  - *Rapporto* il lunedì;
  - *Prova* a mano.
  - L'invio parte solo con la variabile `INVIA_PREVISIONI=si`.

## Controlli

| Controllo | Esito |
|---|---|
| Test | **50 passati** (`uv run pytest -q`): combinazione delle previsioni, distribuzione numerica (12 casi difficili, aperti e chiusi), date, tetti di spesa, registro, rapporto, bot completo, guasti. Il template non ha test propri. |
| Prova senza invio | Fatta con il **modello finto**, perché non c'è nessun `.env` né chiavi: una previsione valida per ogni tipo. Sì/no 0,34; numerica p10-p90 54-117; scelta multipla con 4 opzioni che sommano a 1; data p10-p90 da 2027-01-02 a 2027-07-11. |
| Costo misurato | 9 chiamate a domanda, circa 76.000 token letti e 12.000 scritti, cioè **0,43 $ a domanda**. I token sono stimati dalla lunghezza dei testi e la ricerca web è simulata con 15.000 token letti per giro. |
| Guasti | Con AskNews guasta (`--guasto`) le 4 previsioni vengono fatte tutte. Con tutte le fonti giù si usa la conoscenza del modello, dichiarandolo. Con il modello giù la domanda viene saltata e registrata, senza blocchi. |
| Tetti | Al tetto mensile la domanda viene saltata senza spendere. Il tetto per domanda la ferma: le chiamate già partite in parallelo possono sforarlo di poco, al massimo di una previsione. |
| Segreti | Nessuna chiave nel codice né nella storia git (scansione `git grep` pulita). `.env` è escluso, e le chiavi si leggono solo dall'ambiente. |
| Minuti GitHub | Un giro a vuoto dura circa 3 secondi in locale e circa 30-40 su GitHub, quindi conta 1 minuto. Ogni ora fanno 720 minuti, più 250-400 per le domande: **circa 1.000-1.150 minuti su 2.000**. |
| Regole | Nessun passaggio umano sulle domande aperte. L'utente accende l'invio una volta, prima di iniziare. |

## Costo stimato

- **Per domanda:** circa 0,43 $ (0,36 $ quando il secondo giro di ricerca non serve).
- **Al mese:** circa 90-110 $ per circa 250 domande. Il tetto è 120 $, configurabile.

**Da verificare subito (pianificatore):** il piano dà per scontato che Max includa crediti API. Per quanto so, l'abbonamento Max e la Console API si pagano a parte, quindi se non arrivano i crediti gratuiti di Metaculus i ~100 $ al mese sono soldi veri. In quel caso conviene:

- abbassare `TETTO_MESE_USD`;
- oppure usare solo il proxy Metaculus o OpenRouter con i crediti gratuiti.

## Passi dell'utente, in ordine

1. **Metaculus (10 min):** crea l'account, poi il bot, e copia il token.
2. **Modulo di partecipazione e richiesta crediti (10 min).**
3. **AskNews (facoltativo, 5 min).**
4. **Chiave della Console Anthropic (5 min).**
5. **GitHub (10 min):** crea un repository privato, carica la cartella e inserisci i segreti `METACULUS_TOKEN` e `ANTHROPIC_API_KEY`, più `ASKNEWS_CLIENT_ID` e `ASKNEWS_SECRET` se c'è AskNews.
6. **Prova (5 min):** lancia Actions → *Prova* con "domande vere" spuntato e verifica che finisca in verde.
7. **Invio (1 min):** imposta la variabile `INVIA_PREVISIONI` = `si`, **entro domenica 18/10** per il MiniBench del 19.

## Cosa non ho potuto verificare

- **L'invio vero e le chiamate ai modelli veri:** non ci sono token né chiavi, e l'API di Metaculus rifiuta chi non è autenticato (403).
- **La ricerca web di Claude:** strumento `web_search_20260209`, scritta seguendo la documentazione ma mai eseguita. Non so nemmeno se funziona attraverso il proxy Metaculus.
- **I nomi dei modelli su OpenRouter** (`claude-opus-5.5`) e la disponibilità di Claude 5.5 sul proxy Metaculus.
- **La classifica nel rapporto:** l'endpoint `/api/leaderboards/project/<slug>/` e il suo formato sono un tentativo. Se non risponde, il rapporto lo scrive e va avanti. Anche il premio stimato con la regola "quadrato del punteggio" non è ufficiale.
- **La finestra di apertura delle domande:** se restano aperte meno di un'ora, il giro orario ne perde alcune. Si vede nel primo rapporto; la soluzione sarebbe un repository pubblico, con minuti illimitati, ogni 20 minuti.
- **La data esatta del MiniBench (19/10) e la fine della stagione:** non verificate.
