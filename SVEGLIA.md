# Chi sveglia il bot

Il 21/10/2026 il cron di GitHub (ogni ora) ha fatto 3 giri in 14 ore: GitHub
esegue i cron programmati quando ha capacita', e li salta spesso. Le domande
del torneo restano aperte poche ore, quindi cosi' se ne perde la maggior parte
(84 domande uscite dal 28/9, una sola presa).

La catena di sveglia ora e' questa, in ordine:

| Chi | Quando | Costo |
|---|---|---|
| cron-job.org (servizio gratuito) | ogni 30 minuti, al minuto 05 e 35 | 0 euro, circa 1.440 minuti GitHub al mese |
| Il tuo PC, `sveglia_bot.ps1` | ogni 10 minuti, ma avvia solo se l'ultimo giro ha piu' di 25 minuti | 0 euro, 0 minuti se cron-job.org funziona |
| Cron di GitHub | ogni 6 ore | circa 120 minuti al mese |

Minuti gratuiti: 2.000 al mese per i repository privati; ogni giro vale almeno
1 minuto anche se dura 35 secondi. Totale previsto: circa 1.850 al mese.
Se un giorno vuoi la sveglia ogni 15 minuti, basta rendere pubblico il
repository: i minuti diventano illimitati. Il codice del bot e' gia' basato sul
template pubblico di Metaculus; le chiavi stanno nei segreti e non si vedono.

## 1. Il token GitHub (lo crei tu, 3 minuti)

Serve un token "a grana fine" che puo' fare una cosa sola: avviare i workflow di
questo repository.

1. Apri https://github.com/settings/personal-access-tokens/new
2. **Token name:** `cron-job bot-previsioni`
3. **Expiration:** 366 giorni (segnati la data: va rifatto tra un anno)
4. **Repository access:** "Only select repositories" e scegli `bot-previsioni`
5. **Permissions > Repository permissions > Actions:** "Read and write"
   (Metadata "Read-only" si attiva da solo)
6. **Generate token** e copia la stringa che inizia con `github_pat_`.
   Si vede una volta sola. Non incollarla in chat: va solo dentro cron-job.org.

## 2. cron-job.org (lo fai tu, 5 minuti)

1. Apri https://cron-job.org e crea un account con la tua email (gratuito).
2. **Create cronjob**:
   - **Title:** `Sveglia bot previsioni`
   - **URL:** `https://api.github.com/repos/Alessandro-barrale/bot-previsioni/actions/workflows/previsioni.yaml/dispatches`
   - **Schedule:** "Custom": minuti `5` e `35`, ore "every hour", giorni "every day".
3. Scheda **Advanced**:
   - **Request method:** `POST`
   - **Headers** (uno per riga, chiave e valore):
     - `Accept` = `application/vnd.github+json`
     - `Authorization` = `Bearer github_pat_...` (il tuo token)
     - `X-GitHub-Api-Version` = `2022-11-28`
     - `Content-Type` = `application/json`
   - **Request body:** `{"ref":"main"}`
4. **Create**, poi dalla pagina del job premi **Test run**: la risposta giusta e'
   `204 No Content`. Se vedi `401` il token e' sbagliato, se vedi `404` il token
   non ha il permesso Actions sul repository.

## 3. Controllo

Dal PC, in una finestra qualsiasi:

```bash
gh run list --repo Alessandro-barrale/bot-previsioni --limit 10
```

Ogni mezz'ora deve comparire un giro con evento `workflow_dispatch`.
Il diario della sveglia dal PC e' in `dati\sveglia.log`.

## 4. Il PC come riserva

L'attivita' pianificata "Sveglia bot previsioni" gira ogni 10 minuti quando sei
collegato. Non ha bisogno di chiavi: usa `gh`, che e' gia' autenticato sul tuo
account, e chiede a GitHub di avviare il workflow. Per vederla o toglierla:

```bash
schtasks /Query /TN "Sveglia bot previsioni"
```

```bash
schtasks /Delete /TN "Sveglia bot previsioni" /F
```
