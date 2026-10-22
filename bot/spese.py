"""
Registro SQLite di ogni chiamata ai modelli e di ogni previsione, più i tetti
di spesa per domanda, per giorno e per mese.

Il controllo dei tetti avviene PRIMA di ogni chiamata: se un tetto è già
raggiunto la chiamata non parte e viene sollevato TettoSuperato. Una domanda
che resta senza previsioni viene saltata (vale zero punti, ma non si spende).

Ogni chiamata porta anche il torneo (contesto `torneo_corrente`): così un
torneo può avere tetti suoi (Market Pulse: mensile e giornaliero propri),
contati sullo stesso registro filtrando per torneo. Nei tornei con più
previsioni per domanda (Market Pulse) il tetto per domanda vale per ogni
passata (contesto `inizio_passata`), con un tetto sul totale della domanda.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Prezzi in dollari per milione di token (ingresso, uscita), listino Anthropic
# ottobre 2026. Servono quando il fornitore non restituisce il costo.
PREZZI_PER_MILIONE: dict[str, tuple[float, float]] = {
    "opus-5-5": (4.00, 20.00),
    "opus-5.5": (4.00, 20.00),
    "sonnet-5-5": (2.00, 10.00),
    "sonnet-5.5": (2.00, 10.00),
    "haiku-5-5": (0.10, 0.50),
    "haiku-5.5": (0.10, 0.50),
    # OpenRouter, listino letto da /api/v1/models il 22/10/2026
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-sol": (2.00, 10.00),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-4-argon": (2.00, 10.00),
    "gemini-3.5-flash": (1.50, 9.00),
}
PREZZO_PREDEFINITO = (5.00, 25.00)  # modello sconosciuto: stima prudente
COSTO_RICERCA_WEB = 0.01  # 10 $ ogni 1.000 ricerche

domanda_corrente: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "domanda_corrente", default=None
)
# Torneo della domanda in corso: finisce nella colonna `torneo` delle chiamate.
torneo_corrente: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "torneo_corrente", default=None
)
# Inizio della passata in corso (ISO, UTC) quando una domanda si prevede più volte:
# il tetto per domanda conta solo le chiamate da quel momento. None = tutta la storia.
inizio_passata: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "inizio_passata", default=None
)


@dataclass
class TettiTorneo:
    """Tetti propri di un torneo, in dollari, oltre a quelli generali (None = nessuno)."""

    mese: float | None = None
    giorno: float | None = None
    # Tetto sul totale di una domanda, tutte le passate insieme.
    domanda_totale: float | None = None


class TettoSuperato(RuntimeError):
    pass


def stima_costo(modello: str, token_ingresso: int, token_uscita: int, ricerche_web: int = 0) -> float:
    prezzo = PREZZO_PREDEFINITO
    for chiave, valore in PREZZI_PER_MILIONE.items():
        if chiave in modello:
            prezzo = valore
            break
    return (
        token_ingresso * prezzo[0] / 1_000_000
        + token_uscita * prezzo[1] / 1_000_000
        + ricerche_web * COSTO_RICERCA_WEB
    )


def _adesso() -> datetime:
    return datetime.now(timezone.utc)


class Registro:
    def __init__(
        self,
        percorso: Path,
        tetto_domanda: float,
        tetto_giorno: float,
        tetto_mese: float,
        tetti_torneo: dict[str, TettiTorneo] | None = None,
    ) -> None:
        self.percorso = Path(percorso)
        self.percorso.parent.mkdir(parents=True, exist_ok=True)
        self.tetto_domanda = tetto_domanda
        self.tetto_giorno = tetto_giorno
        self.tetto_mese = tetto_mese
        self.tetti_torneo: dict[str, TettiTorneo] = dict(tetti_torneo or {})
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.percorso), check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chiamate (
                id INTEGER PRIMARY KEY,
                momento TEXT NOT NULL,
                domanda TEXT,
                scopo TEXT,
                modello TEXT NOT NULL,
                token_ingresso INTEGER NOT NULL,
                token_uscita INTEGER NOT NULL,
                ricerche_web INTEGER NOT NULL DEFAULT 0,
                costo REAL NOT NULL,
                esito TEXT NOT NULL DEFAULT 'ok',
                torneo TEXT
            );
            CREATE TABLE IF NOT EXISTS previsioni (
                id INTEGER PRIMARY KEY,
                momento TEXT NOT NULL,
                domanda TEXT NOT NULL,
                url TEXT,
                titolo TEXT,
                torneo TEXT,
                tipo TEXT,
                previsione TEXT,
                costo REAL,
                inviata INTEGER NOT NULL,
                errore TEXT
            );
            CREATE INDEX IF NOT EXISTS chiamate_momento ON chiamate(momento);
            CREATE INDEX IF NOT EXISTS chiamate_domanda ON chiamate(domanda);
            CREATE INDEX IF NOT EXISTS previsioni_domanda ON previsioni(domanda);
            """
        )
        # Registri creati prima di Market Pulse: nelle chiamate manca la colonna torneo.
        colonne = {riga[1] for riga in self._db.execute("PRAGMA table_info(chiamate)")}
        if "torneo" not in colonne:
            self._db.execute("ALTER TABLE chiamate ADD COLUMN torneo TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS chiamate_torneo ON chiamate(torneo)")
        self._db.commit()

    # ------------------------------------------------------------- spesa
    def _somma(self, dove: str, parametri: tuple) -> float:
        with self._lock:
            riga = self._db.execute(
                f"SELECT COALESCE(SUM(costo), 0) FROM chiamate WHERE {dove}", parametri
            ).fetchone()
        return float(riga[0])

    def spesa_domanda(self, domanda: str, dal: str | None = None) -> float:
        """Spesa di una domanda; con `dal` (ISO) solo le chiamate da quel momento (una passata)."""
        if dal is None:
            return self._somma("domanda = ?", (domanda,))
        return self._somma("domanda = ? AND momento >= ?", (domanda, dal))

    def spesa_giorno(self, giorno: datetime | None = None) -> float:
        giorno = giorno or _adesso()
        return self._somma("substr(momento, 1, 10) = ?", (giorno.strftime("%Y-%m-%d"),))

    def spesa_mese(self, giorno: datetime | None = None) -> float:
        giorno = giorno or _adesso()
        return self._somma("substr(momento, 1, 7) = ?", (giorno.strftime("%Y-%m"),))

    def spesa_mese_torneo(self, torneo: str, giorno: datetime | None = None) -> float:
        giorno = giorno or _adesso()
        return self._somma("torneo = ? AND substr(momento, 1, 7) = ?", (torneo, giorno.strftime("%Y-%m")))

    def spesa_giorno_torneo(self, torneo: str, giorno: datetime | None = None) -> float:
        giorno = giorno or _adesso()
        return self._somma("torneo = ? AND substr(momento, 1, 10) = ?", (torneo, giorno.strftime("%Y-%m-%d")))

    def controlla_tetti(self, domanda: str | None = None) -> None:
        """Solleva TettoSuperato se una nuova chiamata sforerebbe un tetto."""
        mese = self.spesa_mese()
        if mese >= self.tetto_mese:
            raise TettoSuperato(f"tetto mensile raggiunto: {mese:.2f} $ su {self.tetto_mese:.2f} $")
        giorno = self.spesa_giorno()
        if giorno >= self.tetto_giorno:
            raise TettoSuperato(f"tetto giornaliero raggiunto: {giorno:.2f} $ su {self.tetto_giorno:.2f} $")
        torneo = torneo_corrente.get()
        propri = self.tetti_torneo.get(torneo) if torneo else None
        if propri is not None:
            if propri.mese is not None:
                spesa = self.spesa_mese_torneo(torneo)
                if spesa >= propri.mese:
                    raise TettoSuperato(f"tetto mensile {torneo} raggiunto: {spesa:.2f} $ su {propri.mese:.2f} $")
            if propri.giorno is not None:
                spesa = self.spesa_giorno_torneo(torneo)
                if spesa >= propri.giorno:
                    raise TettoSuperato(f"tetto giornaliero {torneo} raggiunto: {spesa:.2f} $ su {propri.giorno:.2f} $")
        if domanda is not None:
            spesa = self.spesa_domanda(domanda, dal=inizio_passata.get())
            if spesa >= self.tetto_domanda:
                raise TettoSuperato(
                    f"tetto per domanda raggiunto ({domanda}): {spesa:.2f} $ su {self.tetto_domanda:.2f} $"
                )
            if propri is not None and propri.domanda_totale is not None:
                totale = self.spesa_domanda(domanda)
                if totale >= propri.domanda_totale:
                    raise TettoSuperato(
                        f"tetto per domanda (tutte le passate) raggiunto ({domanda}): "
                        f"{totale:.2f} $ su {propri.domanda_totale:.2f} $"
                    )

    def registra_chiamata(
        self,
        modello: str,
        token_ingresso: int,
        token_uscita: int,
        costo: float | None = None,
        ricerche_web: int = 0,
        scopo: str = "",
        domanda: str | None = None,
        esito: str = "ok",
        torneo: str | None = None,
    ) -> float:
        if domanda is None:
            domanda = domanda_corrente.get()
        if torneo is None:
            torneo = torneo_corrente.get()
        if costo is None:  # costo sconosciuto: si stima dal listino
            costo = stima_costo(modello, token_ingresso, token_uscita, ricerche_web)
        with self._lock:
            self._db.execute(
                "INSERT INTO chiamate (momento, domanda, scopo, modello, token_ingresso,"
                " token_uscita, ricerche_web, costo, esito, torneo) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _adesso().isoformat(timespec="seconds"),
                    domanda,
                    scopo,
                    modello,
                    int(token_ingresso),
                    int(token_uscita),
                    int(ricerche_web),
                    float(costo),
                    esito,
                    torneo,
                ),
            )
            self._db.commit()
        return float(costo)

    # -------------------------------------------------------- previsioni
    def registra_previsione(
        self,
        domanda: str,
        url: str | None,
        titolo: str | None,
        torneo: str | None,
        tipo: str,
        previsione: object | None,
        inviata: bool,
        errore: str | None = None,
        dal: str | None = None,
    ) -> None:
        costo = self.spesa_domanda(domanda, dal=dal)  # con `dal`: il costo di questa passata
        with self._lock:
            self._db.execute(
                "INSERT INTO previsioni (momento, domanda, url, titolo, torneo, tipo,"
                " previsione, costo, inviata, errore) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    _adesso().isoformat(timespec="seconds"),
                    domanda,
                    url,
                    titolo,
                    torneo,
                    tipo,
                    json.dumps(previsione, ensure_ascii=False, default=str) if previsione is not None else None,
                    costo,
                    1 if inviata else 0,
                    errore,
                ),
            )
            self._db.commit()

    def gia_prevista(self, domanda: str, inviata: bool, torneo: str | None = None) -> bool:
        """
        Vero se la domanda ha già una previsione riuscita (inviata o di prova).
        Con `torneo` contano solo le previsioni di quel torneo.
        """
        return bool(self.momenti_previsioni(domanda, inviata, torneo))

    def momenti_previsioni(self, domanda: str, inviata: bool, torneo: str | None = None) -> list[datetime]:
        """Momenti (UTC, in ordine) delle previsioni riuscite di una domanda; con `torneo` solo quelle del torneo."""
        sql = "SELECT momento FROM previsioni WHERE domanda = ? AND errore IS NULL AND inviata = ?"
        parametri: tuple = (domanda, 1 if inviata else 0)
        if torneo is not None:
            sql += " AND torneo = ?"
            parametri += (torneo,)
        with self._lock:
            righe = self._db.execute(sql + " ORDER BY momento", parametri).fetchall()
        momenti = []
        for (momento,) in righe:
            istante = datetime.fromisoformat(momento)
            momenti.append(istante if istante.tzinfo else istante.replace(tzinfo=timezone.utc))
        return momenti

    def interroga(self, sql: str, parametri: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, parametri).fetchall()

    def chiudi(self) -> None:
        with self._lock:
            self._db.close()
