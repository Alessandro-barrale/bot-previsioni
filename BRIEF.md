# Brief: bot di previsioni per i tornei a premi di Metaculus

*14 ottobre 2026. Scelto dal pianificatore con i paletti dati dall'utente: un programma che lavora da solo, risultati visibili in settimane, niente P.IVA, un argomento che non annoi. Catodo è in pausa (il suo brief resta in `..\show\BRIEF_PILOTA.md`).*

Cartella di lavoro: `D:\Idee Claude\obiettivo-200\bot-previsioni\`.

## Obiettivo

Costruire un bot che partecipa da solo ai tornei per bot di Metaculus (FutureEval):
- **Fall 2026:** iniziato il 28 settembre, finisce a inizio gennaio 2027 (data da confermare sulla pagina del torneo). Mette in palio 50.000 $ su 300-500 domande di scienza, tecnologia, geopolitica, economia e AI;
- **MiniBench:** gare di due settimane, ciascuna con circa 60 domande e 1.000 $ di premi.

**Traguardo:** il bot deve essere pronto per il MiniBench che, con il calendario ogni due settimane dal 7 settembre, dovrebbe partire **lunedì 19 ottobre**.

Secondo i partecipanti (fonte non ufficiale), i premi sono proporzionali al quadrato del punteggio e sotto i 50 $ non si paga. Quindi conta stare sopra la media, non solo partecipare.

## Regole del torneo (da rispettare sempre)

- **Nessun intervento umano** sulle previsioni delle domande aperte: il bot è del tutto automatico. Niente rilanci a mano di una domanda aperta, niente correzioni dopo aver visto una previsione.
- **Un solo bot** con diritto ai premi per persona.
- A fine stagione: questionario per chi crea i bot, codice o descrizione da condividere con Metaculus, verifica dell'identità.
- Una domanda saltata vale zero punti: meglio saltarla che inviare una previsione rotta.

## Cosa non fare

- Non creare account e non toccare chiavi o password: account Metaculus, token del bot, repository GitHub e chiavi API li crea l'utente.
- Le chiavi stanno solo nel file `.env` (che non leggi e non stampi) o nei segreti di GitHub. Mai nel codice, nei log, nei rapporti o nei commit.
- Niente previsioni vere sui tornei: solo modalità di prova, senza invio. L'invio lo attiva l'utente.
- Non fare tu previsioni sulle domande: le fa solo il bot.

## Da dove partire

- **Template ufficiale:** https://github.com/Metaculus/metac-bot-template. Usa la libreria `forecasting-tools` e la classe `FallTemplateBot2026`, ha i workflow GitHub Actions per torneo e MiniBench e la modalità `test_questions`.
- **Ambiente:** Python 3.11 o successivo (sul PC ci sono la 3.13 e CPython 3.11 tramite `uv`), con le dipendenze dentro la cartella.
- **Modelli:**
  - Claude tramite API come modello principale: `claude-opus-5-5` per la previsione, `claude-sonnet-5-5` o `claude-haiku-5-5` per riassumere le ricerche;
  - altri modelli tramite OpenRouter, se arrivano i crediti gratuiti di Metaculus.

## Cosa costruire, in ordine

1. **Il template funzionante:** ambiente installato e domande di esempio in modalità prova, senza invio.
2. **Ricerca da più fonti:**
   - AskNews (gratuito, 1.000 chiamate al mese) più la ricerca web del modello;
   - in 2-3 giri: cerca, leggi, poi cerca quello che manca;
   - con la data di ogni fonte;
   - se una fonte fallisce, il bot va avanti con le altre.
3. **Più previsioni per domanda:** almeno 5 previsioni indipendenti, con almeno 2 modelli diversi quando ci sono i crediti. Poi si combinano così:
   - domande sì/no: la mediana;
   - scelte multiple: la media;
   - domande numeriche: una distribuzione pulita e monotona.
4. **Metodo da previsore nel prompt:**
   - quanto spesso succede di solito;
   - quanto tempo manca alla scadenza;
   - cosa succede se non cambia niente.

   Probabilità mai sotto l'1% né sopra il 99%.
5. **Tetti di spesa nel codice**, per domanda, per giorno e per mese, configurabili:
   - obiettivo: in media al massimo 0,40 $ a domanda;
   - tetto mensile predefinito: 120 $;
   - un registro SQLite di ogni chiamata (modello, token, costo stimato) e di ogni previsione.
6. **Rapporto settimanale in italiano** in `rapporti/`: domande coperte, spesa, posizione e punteggio in classifica (se l'API di Metaculus li espone) e premio stimato. È l'unica cosa che l'utente guarda.
7. **GitHub Actions:** i workflow del template, con una frequenza che resti nei 2.000 minuti gratuiti al mese di un repository privato. Misura quanto dura un giro senza domande nuove e scrivi il calcolo.
8. **README in italiano** con i passi dell'utente: i segreti da inserire (con i nomi esatti), come lanciare la prova, come attivare l'invio.

Queste scelte vengono dal questionario di Fall 2025 sui bot vincenti:
- più fonti di ricerca danno punteggi più alti;
- la ricerca a più giri batte quella in un colpo solo;
- l'86% dei vincitori combina più previsioni.

## Cosa fa l'utente (non tu)

- **Su Metaculus:**
  - crea l'account, poi il bot e il suo token dalla pagina "Participate" di FutureEval;
  - compila il modulo di partecipazione e chiede i crediti gratuiti per i modelli.
- **Su AskNews:** crea l'account (facoltativo).
- **Per le chiamate ai modelli:** usa una chiave API della Console collegata ai crediti di Max, oppure la chiave OpenRouter dei crediti di Metaculus.
- **Su GitHub:** crea un repository privato e inserisce i segreti.
- **Per una prova con i modelli veri:** mette le chiavi nel file `.env`.

## Prima di dire "fatto"

| Controllo | Atteso |
|---|---|
| Test | passano quelli del template e i nuovi (combinazione delle previsioni, tetti di spesa, distribuzione numerica) |
| Prova senza invio | una previsione valida per ogni tipo (sì/no, numerica, scelta multipla), con il costo per domanda misurato. Se in `.env` non ci sono chiavi, prova con un modello finto e scrivilo |
| Guasti | se cade una fonte o un modello, il bot prevede con il resto oppure salta la domanda, senza bloccarsi |
| Segreti | nessuna chiave nel codice, nei log, nei rapporti o nella storia di git |
| Minuti GitHub | il calcolo dei minuti al mese con la frequenza scelta |
| Regole | nessun passaggio che richieda una persona sulle domande aperte |

## Alla fine

Scrivi `REPORT.md` in italiano, breve:
- cosa c'è e come si avvia;
- costo stimato per domanda e per mese;
- i passi dell'utente in ordine, con i tempi;
- cosa non hai potuto verificare (per esempio l'invio vero).

Chiudi con un resoconto di al massimo 30 righe. Poi fermati.
