"""
Domyos Woodrower — Logger SQLite

Connexion unique (mode WAL), insertion à chaque paquet BLE avec commit
périodique, et détection automatique du début / de la fin de session
à partir de l'activité du rameur.

Le rameur fige ses dernières valeurs (puissance, allure, cadence) pendant
~15 s après le dernier coup tout en faisant avancer chrono et distance.
Ces points sont marqués `coasting=1` et exclus des moyennes ; distance et
chrono restent ceux du rameur. Le figeage est détecté quand NI le compteur
de coups NI le triplet (puissance, allure, cadence) n'ont bougé depuis
`coasting_after_s` : la console incrémente parfois le compteur avec ~10 s
de retard à la reprise, mais les valeurs, elles, varient dès qu'on rame.

Utilisation :
    log = SessionLogger()          # ouvre la DB (init du schéma incluse)
    event = log.record(parsed)     # à chaque notify ; renvoie
                                   # "started" | "stopped" | None
    log.close()                    # à l'arrêt du process (clôture la
                                   # session en cours si besoin)
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "domyos.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    duration_s       INTEGER,
    distance_m       INTEGER,
    strokes          INTEGER,
    calories         INTEGER,
    avg_power_w      REAL,
    avg_pace_s       INTEGER,
    avg_stroke_rate  REAL,
    max_power_w      INTEGER,
    resistance_used  TEXT   -- JSON : liste des niveaux de résistance utilisés
);

CREATE TABLE IF NOT EXISTS datapoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    ts            TEXT NOT NULL,
    elapsed_s     INTEGER,
    stroke_rate   REAL,
    stroke_count  INTEGER,
    distance_m    INTEGER,
    pace_s        INTEGER,
    power_w       INTEGER,
    calories      INTEGER,
    heart_rate    INTEGER,
    resistance    INTEGER,
    coasting      INTEGER DEFAULT 0   -- 1 = valeurs figées (plus de coup depuis > 5 s)
);

CREATE INDEX IF NOT EXISTS idx_dp_session ON datapoints(session_id);
CREATE INDEX IF NOT EXISTS idx_dp_ts      ON datapoints(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path = DB_PATH) -> None:
    """Crée le schéma si absent (idempotent)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(datapoints)")}
        if "coasting" not in cols:
            conn.execute("ALTER TABLE datapoints ADD COLUMN coasting INTEGER DEFAULT 0")


class SessionLogger:
    """
    Détection de session :
      - démarre au premier paquet "actif" (coups > 0 ou temps > 0 ou puissance > 0)
      - s'arrête si les compteurs du rameur repartent à zéro (reset machine),
        sur appel explicite à stop() (stop/reset machine, déconnexion BLE),
        ou si le chrono n'a pas avancé depuis `idle_timeout_s` HORS pause
      - `paused` (positionné par le serveur sur Fitness Machine Status)
        suspend le timeout ; il est levé dès que le chrono repart
      - une session sans aucun coup compté (`min_strokes`) est supprimée à la
        clôture, datapoints compris
      - après un reset, le rameur remet le chrono à zéro un ou deux paquets
        AVANT le compteur de coups : ce compteur résiduel (égal au total de la
        séance précédente) ne suffit pas à ouvrir une nouvelle session
    """

    def __init__(self, db_path: Path = DB_PATH,
                 idle_timeout_s: float = 300.0,
                 coasting_after_s: float = 5.0,
                 commit_interval_s: float = 1.0,
                 min_strokes: int = 1):
        self.db_path = db_path
        self.idle_timeout_s = idle_timeout_s
        self.coasting_after_s = coasting_after_s
        self.commit_interval_s = commit_interval_s
        self.min_strokes = min_strokes

        init_db(db_path)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self.session_id: int | None = None
        self._rows: list[dict] = []
        self._last_commit = time.monotonic()
        self._last_progress = time.monotonic()   # dernier paquet où elapsed_s a avancé
        self._last_stroke_change = time.monotonic()   # dernier paquet où stroke_count a changé
        self._last_sig_change = time.monotonic()      # dernier paquet où (puissance, allure, cadence) a changé
        self._last_sig: tuple | None = None
        self._last_elapsed: int | None = None
        self._last_strokes: int | None = None
        self._stale_strokes: int | None = None   # total de coups de la séance précédente, encore affiché après reset
        self.paused = False
        self.coasting = False
        print(f"[DB] {db_path} ouverte (WAL)")

    # ── API publique ──────────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return self.session_id is not None

    def record(self, d: dict) -> str | None:
        """
        Traite un paquet parsé (dict de ftms.parse_rower_data).
        Renvoie "started" / "stopped" quand une session change d'état, sinon None.
        """
        elapsed = d.get("elapsed_s")
        strokes = d.get("stroke_count")
        power = d.get("power_w")
        event = None

        if self.active:
            reset = (
                (elapsed is not None and self._last_elapsed is not None and elapsed < self._last_elapsed)
                or (strokes is not None and self._last_strokes is not None and strokes < self._last_strokes)
            )
            if reset:
                self.stop()
                event = "stopped"
        if not self.active:
            if not strokes:
                self._stale_strokes = None   # le compteur est bien retombé à zéro
            fresh_strokes = bool(strokes) and strokes != self._stale_strokes
            if elapsed or power or d.get("stroke_rate_spm") or fresh_strokes:
                self.start()
                event = "started"

        if not self.active:
            return event

        now = time.monotonic()
        if elapsed is not None and elapsed != self._last_elapsed:
            self._last_progress = now
            self.paused = False
        if strokes != self._last_strokes:
            self._last_stroke_change = now
        sig = (power, d.get("pace_s_per_500m"), d.get("stroke_rate_spm"))
        if sig != self._last_sig:
            self._last_sig_change = now
        self._last_elapsed = elapsed
        self._last_strokes = strokes
        self._last_sig = sig
        last_activity = max(self._last_stroke_change, self._last_sig_change)
        self.coasting = (now - last_activity) > self.coasting_after_s

        row = {
            "session_id":   self.session_id,
            "ts":           _now(),
            "elapsed_s":    elapsed,
            "stroke_rate":  d.get("stroke_rate_spm"),
            "stroke_count": strokes,
            "distance_m":   d.get("distance_m"),
            "pace_s":       d.get("pace_s_per_500m"),
            "power_w":      power,
            "calories":     d.get("energy_total_kcal"),
            "heart_rate":   d.get("heart_rate_bpm"),
            "resistance":   d.get("resistance"),
            "coasting":     int(self.coasting),
        }
        self._rows.append(row)
        self._conn.execute("""
            INSERT INTO datapoints
                (session_id, ts, elapsed_s, stroke_rate, stroke_count, distance_m,
                 pace_s, power_w, calories, heart_rate, resistance, coasting)
            VALUES
                (:session_id, :ts, :elapsed_s, :stroke_rate, :stroke_count, :distance_m,
                 :pace_s, :power_w, :calories, :heart_rate, :resistance, :coasting)
        """, row)

        if now - self._last_commit >= self.commit_interval_s:
            self._conn.commit()
            self._last_commit = now

        if not self.paused and now - self._last_progress >= self.idle_timeout_s:
            self.stop()
            event = "stopped"

        return event

    def start(self) -> None:
        """Ouvre une session en base. Appelé automatiquement par record()."""
        if self.active:
            return
        started_at = _now()
        cur = self._conn.execute("INSERT INTO sessions (started_at) VALUES (?)", (started_at,))
        self._conn.commit()
        self.session_id = cur.lastrowid
        self._rows = []
        self._last_elapsed = None
        self._last_strokes = None
        self._last_progress = self._last_stroke_change = self._last_sig_change = time.monotonic()
        self._last_sig = None
        self.paused = False
        self.coasting = False
        print(f"[DB] Session #{self.session_id} démarrée à {started_at}")

    def stop(self) -> dict | None:
        """Clôture la session courante avec ses agrégats. Renvoie les stats."""
        if not self.active:
            return None
        sid = self.session_id
        self.session_id = None
        self.paused = False
        self.coasting = False
        rows = self._rows
        self._rows = []

        last = rows[-1] if rows else None
        self._stale_strokes = last["stroke_count"] if last else None
        if not rows or (last["stroke_count"] or 0) < self.min_strokes:
            self._conn.execute("DELETE FROM datapoints WHERE session_id = ?", (sid,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            self._conn.commit()
            print(f"[DB] Session #{sid} sans coup ({len(rows)} points), supprimée.")
            return None

        # Moyennes sur les points "vivants" : ni nuls (pas en mouvement),
        # ni figés par le rameur après le dernier coup (coasting).
        live         = [r for r in rows if not r["coasting"]]
        powers       = [r["power_w"]     for r in live if r["power_w"]]
        paces        = [r["pace_s"]      for r in live if r["pace_s"]]
        stroke_rates = [r["stroke_rate"] for r in live if r["stroke_rate"]]
        resistances  = sorted({r["resistance"] for r in rows if r["resistance"]})

        stats = {
            "id":              sid,
            "ended_at":        _now(),
            "duration_s":      last["elapsed_s"],
            "distance_m":      last["distance_m"],
            "strokes":         last["stroke_count"],
            "calories":        last["calories"],
            "avg_power_w":     round(sum(powers) / len(powers), 1) if powers else None,
            "avg_pace_s":      round(sum(paces) / len(paces)) if paces else None,
            "avg_stroke_rate": round(sum(stroke_rates) / len(stroke_rates), 1) if stroke_rates else None,
            "max_power_w":     max(powers) if powers else None,
            "resistance_used": json.dumps(resistances),
        }
        self._conn.execute("""
            UPDATE sessions SET
                ended_at = :ended_at, duration_s = :duration_s,
                distance_m = :distance_m, strokes = :strokes, calories = :calories,
                avg_power_w = :avg_power_w, avg_pace_s = :avg_pace_s,
                avg_stroke_rate = :avg_stroke_rate, max_power_w = :max_power_w,
                resistance_used = :resistance_used
            WHERE id = :id
        """, stats)
        self._conn.commit()

        t = stats["duration_s"] or 0
        print(f"[DB] Session #{sid} terminée — {t // 60:02d}:{t % 60:02d} | "
              f"{stats['distance_m']} m | {stats['calories']} kcal | "
              f"{stats['avg_power_w']} W moy. | {len(rows)} points "
              f"dont {len(rows) - len(live)} figés")
        return stats

    def close(self) -> None:
        """Clôture la session en cours et ferme la connexion."""
        self.stop()
        self._conn.commit()
        self._conn.close()
