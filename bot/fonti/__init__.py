"""
Fonti aggiuntive della ricerca v2, ognuna con interruttore, timeout e
fallimento non bloccante:

- fonte_risoluzione: le pagine citate nei criteri di risoluzione, lette ORA;
- mercati: Polymarket, Manifold e Kalshi (API pubbliche gratuite), pesati per liquidità;
- serie_storiche: FRED (API con FRED_API_KEY, altrimenti il CSV pubblico) e Yahoo Finance per le domande numeriche;
- fred: il CSV pubblico di FRED senza chiave, usato dalla fonte di risoluzione e dalle serie storiche.
"""
