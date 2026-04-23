import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime


def _now() -> str:
    """Current local time as ISO string (respects TZ env var)."""
    return datetime.now().isoformat(timespec="seconds")

DB_PATH = os.environ.get("DB_PATH", "/data/bookclub.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS hc_books (
            id          INTEGER PRIMARY KEY,
            title       TEXT NOT NULL,
            author      TEXT,
            series      TEXT,
            series_pos  REAL,
            cover_url   TEXT,
            status_id   INTEGER,
            rating      REAL,
            synced_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            hc_book_id          INTEGER REFERENCES hc_books(id),
            title               TEXT NOT NULL,
            author              TEXT,
            series              TEXT,
            type                TEXT DEFAULT 'Book',
            audiobook_available TEXT DEFAULT 'Unknown',
            in_abs_library      INTEGER DEFAULT 0,
            abs_progress        REAL,
            abs_finished        INTEGER DEFAULT 0,
            reason              TEXT,
            source              TEXT DEFAULT 'claude',
            -- legacy per-user columns kept for migration, not used for new data
            user_status         TEXT DEFAULT 'pending',
            user_rating         INTEGER,
            user_notes          TEXT,
            created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rec_interactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id  INTEGER NOT NULL REFERENCES profiles(id),
            rec_id      INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
            user_status TEXT NOT NULL DEFAULT 'pending',
            user_rating INTEGER,
            user_notes  TEXT,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(profile_id, rec_id)
        );

        CREATE TABLE IF NOT EXISTS queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id      INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
            profile_id  INTEGER NOT NULL DEFAULT 1 REFERENCES profiles(id),
            position    INTEGER NOT NULL,
            notes       TEXT,
            added_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME,
            hc_synced   INTEGER DEFAULT 0,
            abs_synced  INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'running',
            message     TEXT
        );

        CREATE TABLE IF NOT EXISTS app_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            level       TEXT NOT NULL DEFAULT 'info',
            component   TEXT NOT NULL,
            message     TEXT NOT NULL,
            detail      TEXT
        );
        """)

    # Column migrations (ALTER TABLE not supported in executescript)
    with db() as conn:
        for sql in [
            "ALTER TABLE queue ADD COLUMN profile_id INTEGER NOT NULL DEFAULT 1 REFERENCES profiles(id)",
            "ALTER TABLE recommendations ADD COLUMN user_notes TEXT",
            "ALTER TABLE profiles ADD COLUMN preferences TEXT",
            "ALTER TABLE recommendations ADD COLUMN tags TEXT",
            "ALTER TABLE recommendations ADD COLUMN cover_url TEXT",
            "ALTER TABLE recommendations ADD COLUMN abs_library_item_id TEXT",
            "ALTER TABLE recommendations ADD COLUMN abs_description TEXT",
            "ALTER TABLE recommendations ADD COLUMN abs_duration REAL",
            "ALTER TABLE recommendations ADD COLUMN abs_narrator TEXT",
            "ALTER TABLE recommendations ADD COLUMN abs_genres TEXT",
            "ALTER TABLE recommendations ADD COLUMN abs_series_seq TEXT",
            "ALTER TABLE recommendations ADD COLUMN confidence REAL",
            "ALTER TABLE profiles ADD COLUMN abs_token TEXT",
            "ALTER TABLE profiles ADD COLUMN abs_picks_playlist_id TEXT",
        ]:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

    # Data migrations
    with db() as conn:
        # Seed default profile
        conn.execute("INSERT OR IGNORE INTO profiles (id, name) VALUES (1, 'Nick')")

        # Migrate existing rec interaction data into rec_interactions for profile 1
        conn.execute("""
            INSERT OR IGNORE INTO rec_interactions (profile_id, rec_id, user_status, user_rating, user_notes, updated_at)
            SELECT 1, id, user_status, user_rating, user_notes, updated_at
            FROM recommendations
            WHERE user_status != 'pending'
               OR user_rating IS NOT NULL
               OR user_notes IS NOT NULL
        """)

        # Ensure all queued recs have a 'queued' interaction for profile 1
        conn.execute("""
            INSERT OR IGNORE INTO rec_interactions (profile_id, rec_id, user_status)
            SELECT DISTINCT q.profile_id, q.rec_id, 'queued'
            FROM queue q
        """)


# ---------------------------------------------------------------------------
# Profile queries
# ---------------------------------------------------------------------------

def get_profiles() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()


def get_profile(profile_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()


def create_profile(name: str) -> int:
    with db() as conn:
        cur = conn.execute("INSERT INTO profiles (name) VALUES (?)", (name,))
        return cur.lastrowid


def rename_profile(profile_id: int, name: str):
    with db() as conn:
        conn.execute("UPDATE profiles SET name = ? WHERE id = ?", (name, profile_id))


def update_profile_preferences(profile_id: int, preferences: str):
    with db() as conn:
        conn.execute(
            "UPDATE profiles SET preferences = ? WHERE id = ?",
            (preferences.strip() or None, profile_id)
        )


def update_profile_abs_token(profile_id: int, token: str):
    with db() as conn:
        conn.execute(
            "UPDATE profiles SET abs_token = ? WHERE id = ?",
            (token.strip() or None, profile_id)
        )


def update_profile_picks_playlist_id(profile_id: int, playlist_id: str | None):
    with db() as conn:
        conn.execute(
            "UPDATE profiles SET abs_picks_playlist_id = ? WHERE id = ?",
            (playlist_id, profile_id)
        )


# ---------------------------------------------------------------------------
# Recommendation queries
# ---------------------------------------------------------------------------

_REC_COLS = """
    r.id, r.hc_book_id, r.title, r.author, r.series, r.type,
    r.audiobook_available, r.in_abs_library, r.abs_progress, r.abs_finished,
    r.reason, r.tags, r.source, r.confidence, r.created_at, r.updated_at,
    COALESCE(ri.user_status, 'pending') AS user_status,
    ri.user_rating                      AS user_rating,
    ri.user_notes                       AS user_notes,
    q.id                                AS queue_id,
    q.position                          AS queue_pos,
    COALESCE(
        h.cover_url,
        (SELECT h2.cover_url FROM hc_books h2
         WHERE r.series IS NOT NULL AND r.series != ''
           AND h2.series = r.series AND h2.series_pos > 0
         ORDER BY h2.series_pos ASC LIMIT 1),
        r.cover_url
    ) AS cover_url,
    CASE WHEN h.status_id = 1
              OR EXISTS(SELECT 1 FROM hc_books h3
                        WHERE lower(h3.title) = lower(r.title) AND h3.status_id = 1)
         THEN 1 ELSE 0 END AS on_want_to_read
"""

_REC_JOINS = """
    FROM recommendations r
    LEFT JOIN rec_interactions ri ON ri.rec_id = r.id AND ri.profile_id = :pid
    LEFT JOIN queue q             ON q.rec_id  = r.id AND q.profile_id  = :pid
    LEFT JOIN hc_books h          ON h.id = r.hc_book_id
"""


def get_recommendations(status_filter: str = "all", profile_id: int = 1, q: str = "") -> list[sqlite3.Row]:
    with db() as conn:
        conditions = []
        params: dict = {"pid": profile_id, "status": status_filter}

        if status_filter == "in_library":
            conditions.append("r.in_abs_library = 1 AND COALESCE(ri.user_status, 'pending') = 'pending'")
        elif status_filter == "all":
            conditions.append("COALESCE(ri.user_status, 'pending') NOT IN ('pass', 'read', 'queued')")
        elif status_filter == "archive":
            conditions.append("COALESCE(ri.user_status, 'pending') IN ('pass', 'read')")
        else:
            conditions.append("COALESCE(ri.user_status, 'pending') = :status")

        if q:
            conditions.append(
                "(lower(r.title) LIKE :q OR lower(COALESCE(r.author,'')) LIKE :q "
                "OR lower(COALESCE(r.series,'')) LIKE :q)"
            )
            params["q"] = f"%{q.lower()}%"

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return conn.execute(f"""
            SELECT {_REC_COLS}
            {_REC_JOINS}
            {where}
            ORDER BY
              CASE COALESCE(ri.user_status, 'pending')
                WHEN 'queued'  THEN 1
                WHEN 'pending' THEN 2
                WHEN 'read'    THEN 3
                WHEN 'pass'    THEN 4
              END,
              r.in_abs_library DESC,
              r.title
        """, params).fetchall()


def get_recommendation(rec_id: int, profile_id: int = 1) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(f"""
            SELECT {_REC_COLS}
            {_REC_JOINS}
            WHERE r.id = :rec_id
        """, {"pid": profile_id, "rec_id": rec_id}).fetchone()


def upsert_recommendation(title, author, series, type_, audiobook_available, reason,
                          source="claude", tags=None, confidence=None):
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM recommendations WHERE lower(title) = lower(?)", (title,)
        ).fetchone()
        if existing:
            if confidence is not None:
                conn.execute("UPDATE recommendations SET confidence = ? WHERE id = ?",
                             (confidence, existing["id"]))
            return existing["id"]
        cur = conn.execute("""
            INSERT INTO recommendations (title, author, series, type, audiobook_available,
                                         reason, source, tags, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (title, author, series, type_, audiobook_available, reason, source, tags, confidence))
        return cur.lastrowid


def get_bookclub_picks(profile_id: int) -> list[sqlite3.Row]:
    """
    AI-sourced recommendations in the ABS library, not yet read or passed,
    ordered by confidence DESC (NULLs last).
    """
    with db() as conn:
        return conn.execute("""
            SELECT r.id, r.title, r.author, r.abs_library_item_id, r.confidence,
                   COALESCE(ri.user_status, 'pending') AS user_status
            FROM recommendations r
            LEFT JOIN rec_interactions ri
                   ON ri.rec_id = r.id AND ri.profile_id = ?
            WHERE r.in_abs_library = 1
              AND r.abs_library_item_id IS NOT NULL
              AND r.source IN ('claude', 'claude-api')
              AND COALESCE(ri.user_status, 'pending') NOT IN ('read', 'pass')
            ORDER BY r.confidence DESC NULLS LAST, r.created_at ASC
        """, (profile_id,)).fetchall()


def set_rec_status(rec_id: int, status: str, profile_id: int = 1):
    with db() as conn:
        conn.execute("""
            INSERT INTO rec_interactions (profile_id, rec_id, user_status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id, rec_id) DO UPDATE SET
                user_status = excluded.user_status,
                updated_at  = excluded.updated_at
        """, (profile_id, rec_id, status, _now()))


def set_rec_rating(rec_id: int, rating: int | None, profile_id: int = 1):
    with db() as conn:
        conn.execute("""
            INSERT INTO rec_interactions (profile_id, rec_id, user_status, user_rating, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            ON CONFLICT(profile_id, rec_id) DO UPDATE SET
                user_rating = excluded.user_rating,
                updated_at  = excluded.updated_at
        """, (profile_id, rec_id, rating, _now()))


def set_rec_notes(rec_id: int, notes: str, profile_id: int = 1):
    with db() as conn:
        conn.execute("""
            INSERT INTO rec_interactions (profile_id, rec_id, user_status, user_notes, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            ON CONFLICT(profile_id, rec_id) DO UPDATE SET
                user_notes = excluded.user_notes,
                updated_at = excluded.updated_at
        """, (profile_id, rec_id, notes.strip() or None, _now()))


# ---------------------------------------------------------------------------
# Queue queries
# ---------------------------------------------------------------------------

def get_queue(profile_id: int = 1) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("""
            SELECT q.*, r.title, r.author, r.series, r.type,
                   r.audiobook_available, r.in_abs_library, r.abs_finished,
                   r.abs_library_item_id, r.abs_duration, r.abs_narrator,
                   r.abs_genres, r.abs_series_seq,
                   r.reason, ri.user_status, ri.user_rating, r.hc_book_id,
                   COALESCE(h.cover_url, r.cover_url) AS cover_url
            FROM queue q
            JOIN recommendations r ON r.id = q.rec_id
            LEFT JOIN rec_interactions ri ON ri.rec_id = r.id AND ri.profile_id = q.profile_id
            LEFT JOIN hc_books h ON h.id = r.hc_book_id
            WHERE q.profile_id = ?
            ORDER BY q.position
        """, (profile_id,)).fetchall()


def get_rec_detail(rec_id: int, profile_id: int = 1) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(f"""
            SELECT {_REC_COLS},
                   r.abs_description, r.abs_duration, r.abs_narrator,
                   r.abs_genres, r.abs_series_seq
            {_REC_JOINS}
            WHERE r.id = :rec_id
        """, {"pid": profile_id, "rec_id": rec_id}).fetchone()


def add_to_queue(rec_id: int, profile_id: int = 1, notes: str = "") -> int:
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM queue WHERE rec_id = ? AND profile_id = ?", (rec_id, profile_id)
        ).fetchone()
        if existing:
            return existing["id"]

        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM queue WHERE profile_id = ?", (profile_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO queue (rec_id, profile_id, position, notes) VALUES (?, ?, ?, ?)",
            (rec_id, profile_id, max_pos + 1, notes)
        )
        conn.execute("""
            INSERT INTO rec_interactions (profile_id, rec_id, user_status, updated_at)
            VALUES (?, ?, 'queued', ?)
            ON CONFLICT(profile_id, rec_id) DO UPDATE SET
                user_status = 'queued', updated_at = excluded.updated_at
        """, (profile_id, rec_id, _now()))
        return cur.lastrowid


def remove_from_queue(rec_id: int, profile_id: int = 1):
    with db() as conn:
        conn.execute("DELETE FROM queue WHERE rec_id = ? AND profile_id = ?", (rec_id, profile_id))
        conn.execute("""
            UPDATE rec_interactions SET user_status = 'pending', updated_at = ?
            WHERE rec_id = ? AND profile_id = ? AND user_status = 'queued'
        """, (_now(), rec_id, profile_id))
        _reorder_queue(conn, profile_id)


def move_queue_item(queue_id: int, direction: str, profile_id: int = 1):
    with db() as conn:
        item = conn.execute(
            "SELECT * FROM queue WHERE id = ? AND profile_id = ?", (queue_id, profile_id)
        ).fetchone()
        if not item:
            return
        pos = item["position"]
        if direction == "up" and pos > 1:
            swap_pos = pos - 1
        elif direction == "down":
            max_pos = conn.execute(
                "SELECT MAX(position) FROM queue WHERE profile_id = ?", (profile_id,)
            ).fetchone()[0]
            if pos >= max_pos:
                return
            swap_pos = pos + 1
        else:
            return
        conn.execute(
            "UPDATE queue SET position = ? WHERE position = ? AND profile_id = ?",
            (pos, swap_pos, profile_id)
        )
        conn.execute("UPDATE queue SET position = ? WHERE id = ?", (swap_pos, queue_id))


def reorder_queue(rec_ids: list[int], profile_id: int = 1):
    with db() as conn:
        for i, rec_id in enumerate(rec_ids, 1):
            conn.execute(
                "UPDATE queue SET position = ? WHERE rec_id = ? AND profile_id = ?",
                (i, rec_id, profile_id)
            )


def _reorder_queue(conn, profile_id: int = 1):
    rows = conn.execute(
        "SELECT id FROM queue WHERE profile_id = ? ORDER BY position", (profile_id,)
    ).fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute("UPDATE queue SET position = ? WHERE id = ?", (i, row["id"]))


# ---------------------------------------------------------------------------
# ABS playlist sync helpers
# ---------------------------------------------------------------------------

def wipe_queue(profile_id: int = 1):
    """Clear the queue and reset all 'queued' interactions to 'pending'."""
    with db() as conn:
        conn.execute("""
            UPDATE rec_interactions SET user_status = 'pending', updated_at = ?
            WHERE profile_id = ? AND user_status = 'queued'
        """, (_now(), profile_id))
        conn.execute("DELETE FROM queue WHERE profile_id = ?", (profile_id,))


def get_queue_abs_items(profile_id: int = 1) -> list[sqlite3.Row]:
    """Return queue items that have an ABS library item ID, in queue order."""
    with db() as conn:
        return conn.execute("""
            SELECT q.position, r.id as rec_id, r.abs_library_item_id
            FROM queue q
            JOIN recommendations r ON r.id = q.rec_id
            WHERE q.profile_id = ? AND r.abs_library_item_id IS NOT NULL
            ORDER BY q.position
        """, (profile_id,)).fetchall()


def upsert_abs_playlist_item(profile_id: int, title: str, author: str | None,
                              abs_library_item_id: str, position: int,
                              description: str | None = None, duration: float | None = None,
                              narrator: str | None = None, genres: str | None = None,
                              series: str | None = None, series_seq: str | None = None):
    """
    Find or create a recommendation for an ABS playlist item and place it in the queue.
    Returns the rec_id.
    """
    with db() as conn:
        # Check if we already have a rec linked to this ABS item
        row = conn.execute(
            "SELECT id FROM recommendations WHERE abs_library_item_id = ?",
            (abs_library_item_id,)
        ).fetchone()

        if not row:
            # Try fuzzy title match
            row = conn.execute(
                "SELECT id FROM recommendations WHERE lower(title) = lower(?)",
                (title,)
            ).fetchone()

        cover = f"/abs/cover/{abs_library_item_id}"

        if row:
            rec_id = row["id"]
            conn.execute("""
                UPDATE recommendations SET
                    abs_library_item_id = ?,
                    abs_description = COALESCE(?, abs_description),
                    abs_duration    = COALESCE(?, abs_duration),
                    abs_narrator    = COALESCE(?, abs_narrator),
                    abs_genres      = COALESCE(?, abs_genres),
                    abs_series_seq  = COALESCE(?, abs_series_seq),
                    series          = COALESCE(NULLIF(series,''), ?),
                    cover_url       = ?,
                    in_abs_library  = 1
                WHERE id = ?
            """, (abs_library_item_id, description, duration, narrator, genres,
                  series_seq, series, cover, rec_id))
        else:
            cur = conn.execute("""
                INSERT INTO recommendations (title, author, source, abs_library_item_id,
                    in_abs_library, abs_description, abs_duration, abs_narrator,
                    abs_genres, abs_series_seq, series, cover_url)
                VALUES (?, ?, 'abs-playlist', ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """, (title, author, abs_library_item_id, description, duration,
                  narrator, genres, series_seq, series, cover))
            rec_id = cur.lastrowid

        conn.execute("""
            INSERT INTO queue (rec_id, profile_id, position)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
        """, (rec_id, profile_id, position))

        conn.execute("""
            INSERT INTO rec_interactions (profile_id, rec_id, user_status, updated_at)
            VALUES (?, ?, 'queued', ?)
            ON CONFLICT(profile_id, rec_id) DO UPDATE SET
                user_status = 'queued', updated_at = excluded.updated_at
        """, (profile_id, rec_id, _now()))

        return rec_id


def update_rec_abs_library_item_id(rec_id: int, abs_library_item_id: str):
    with db() as conn:
        conn.execute(
            "UPDATE recommendations SET abs_library_item_id = ? WHERE id = ? AND abs_library_item_id IS NULL",
            (abs_library_item_id, rec_id)
        )


def update_rec_abs_data(rec_id: int, *, library_item_id: str, description: str | None,
                         duration: float | None, narrator: str | None,
                         genres: str | None, series: str | None,
                         series_seq: str | None, cover_url: str | None):
    with db() as conn:
        conn.execute("""
            UPDATE recommendations SET
                abs_library_item_id = ?,
                abs_description     = COALESCE(?, abs_description),
                abs_duration        = COALESCE(?, abs_duration),
                abs_narrator        = COALESCE(?, abs_narrator),
                abs_genres          = COALESCE(?, abs_genres),
                abs_series_seq      = COALESCE(?, abs_series_seq),
                series              = COALESCE(NULLIF(series,''), ?),
                cover_url           = ?,
                updated_at          = ?
            WHERE id = ?
        """, (library_item_id, description, duration, narrator, genres,
              series_seq, series, cover_url, _now(), rec_id))


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def upsert_hc_book(book_id, title, author, series, series_pos, cover_url, status_id, rating):
    with db() as conn:
        conn.execute("""
            INSERT INTO hc_books (id, title, author, series, series_pos, cover_url, status_id, rating, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title, author=excluded.author, series=excluded.series,
              series_pos=excluded.series_pos, cover_url=excluded.cover_url,
              status_id=excluded.status_id, rating=excluded.rating,
              synced_at=excluded.synced_at
        """, (book_id, title, author, series, series_pos, cover_url, status_id, rating, _now()))


def update_rec_cover(rec_id: int, cover_url: str):
    with db() as conn:
        conn.execute(
            "UPDATE recommendations SET cover_url = ? WHERE id = ? AND cover_url IS NULL",
            (cover_url, rec_id)
        )


def update_rec_abs_status(rec_id: int, in_library: bool, progress: float, finished: bool):
    with db() as conn:
        conn.execute("""
            UPDATE recommendations
            SET in_abs_library = ?, abs_progress = ?, abs_finished = ?, updated_at = ?
            WHERE id = ?
        """, (1 if in_library else 0, progress, 1 if finished else 0, _now(), rec_id))


def link_rec_to_hc(rec_id: int, hc_book_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE recommendations SET hc_book_id = ? WHERE id = ?",
            (hc_book_id, rec_id)
        )


def get_hc_read_titles() -> set[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT lower(title) FROM hc_books WHERE status_id = 3"
        ).fetchall()
        return {row[0] for row in rows}


def start_sync_log() -> int:
    with db() as conn:
        cur = conn.execute("INSERT INTO sync_log (started_at) VALUES (?)", (_now(),))
        return cur.lastrowid


def finish_sync_log(log_id: int, hc_synced: int, abs_synced: int, status: str, message: str = ""):
    with db() as conn:
        conn.execute("""
            UPDATE sync_log
            SET finished_at = ?, hc_synced = ?, abs_synced = ?, status = ?, message = ?
            WHERE id = ?
        """, (_now(), hc_synced, abs_synced, status, message, log_id))


def get_last_sync() -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()


# ---------------------------------------------------------------------------
# App log
# ---------------------------------------------------------------------------

def log(component: str, message: str, level: str = "info", detail: str | None = None):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO app_log (created_at, level, component, message, detail) VALUES (?,?,?,?,?)",
                (_now(), level, component, message, detail)
            )
    except Exception:
        pass  # never let logging break the app


def get_app_log(limit: int = 200) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM app_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_app_log_since(since_id: int, limit: int = 100) -> list[sqlite3.Row]:
    """Return log entries with id > since_id, oldest first (for streaming display)."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM app_log WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since_id, limit)
        ).fetchall()


def get_latest_log_id() -> int:
    with db() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM app_log").fetchone()
        return row[0]


def get_sync_history(limit: int = 20) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def clear_app_log():
    with db() as conn:
        conn.execute("DELETE FROM app_log")


def get_next_review_rec(profile_id: int, skip_ids: list[int] | None = None) -> sqlite3.Row | None:
    skip_ids = skip_ids or []
    skip_params: dict = {}
    skip_clause = ""
    if skip_ids:
        keys = [f"s{i}" for i in range(len(skip_ids))]
        skip_clause = f"AND r.id NOT IN (:{', :'.join(keys)})"
        skip_params = {k: v for k, v in zip(keys, skip_ids)}

    with db() as conn:
        params = {"pid": profile_id, **skip_params}
        # First: read recs with no rating (forced review)
        row = conn.execute(f"""
            SELECT {_REC_COLS}
            {_REC_JOINS}
            WHERE COALESCE(ri.user_status, 'pending') = 'read'
              AND ri.user_rating IS NULL
              {skip_clause}
            ORDER BY r.title
            LIMIT 1
        """, params).fetchone()
        if row:
            return row
        # Then: pending recs
        return conn.execute(f"""
            SELECT {_REC_COLS}
            {_REC_JOINS}
            WHERE COALESCE(ri.user_status, 'pending') = 'pending'
              {skip_clause}
            ORDER BY r.in_abs_library DESC, r.title
            LIMIT 1
        """, params).fetchone()


def get_stats(profile_id: int = 1) -> dict:
    with db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        queued  = conn.execute("SELECT COUNT(*) FROM queue WHERE profile_id = ?", (profile_id,)).fetchone()[0]
        passed  = conn.execute("SELECT COUNT(*) FROM rec_interactions WHERE profile_id=? AND user_status='pass'", (profile_id,)).fetchone()[0]
        read    = conn.execute("SELECT COUNT(*) FROM rec_interactions WHERE profile_id=? AND user_status='read'", (profile_id,)).fetchone()[0]
        in_lib  = conn.execute("SELECT COUNT(*) FROM recommendations WHERE in_abs_library=1").fetchone()[0]
        hc_read    = conn.execute("SELECT COUNT(*) FROM hc_books WHERE status_id=3").fetchone()[0]
        hc_want    = conn.execute("SELECT COUNT(*) FROM hc_books WHERE status_id=1").fetchone()[0]
        unrated_hc   = conn.execute("SELECT COUNT(*) FROM hc_books WHERE status_id=3 AND (rating IS NULL OR rating=0)").fetchone()[0]
        unrated_recs = conn.execute("SELECT COUNT(*) FROM rec_interactions WHERE profile_id=? AND user_status='read' AND user_rating IS NULL", (profile_id,)).fetchone()[0]
        in_library_pending = conn.execute("""
            SELECT COUNT(*) FROM recommendations r
            LEFT JOIN rec_interactions ri ON ri.rec_id = r.id AND ri.profile_id = ?
            WHERE r.in_abs_library = 1 AND COALESCE(ri.user_status, 'pending') = 'pending'
        """, (profile_id,)).fetchone()[0]
        pending = total - queued - passed - read
        return {
            "total_recs": total,
            "pending": pending,
            "queued": queued,
            "read": read,
            "passed": passed,
            "in_library": in_lib,
            "in_library_pending": in_library_pending,
            "hc_read": hc_read,
            "hc_want": hc_want,
            "unrated_hc": unrated_hc,
            "unrated_recs": unrated_recs,
        }


# ---------------------------------------------------------------------------
# Rating queue helpers
# ---------------------------------------------------------------------------

def get_unrated_recs(profile_id: int = 1) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(f"""
            SELECT {_REC_COLS}
            {_REC_JOINS}
            WHERE COALESCE(ri.user_status, 'pending') = 'read'
              AND ri.user_rating IS NULL
            ORDER BY r.title
        """, {"pid": profile_id}).fetchall()


def get_unrated_hc_books(limit: int = 200) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("""
            SELECT * FROM hc_books
            WHERE status_id = 3 AND (rating IS NULL OR rating = 0)
            ORDER BY title
            LIMIT ?
        """, (limit,)).fetchall()


def rate_hc_book(book_id: int, rating: int | None):
    with db() as conn:
        conn.execute("UPDATE hc_books SET rating = ? WHERE id = ?", (rating, book_id))


# ---------------------------------------------------------------------------
# API context helpers (used by refresh_recs.py host script)
# ---------------------------------------------------------------------------

def get_rec_context(profile_id: int = 1) -> dict:
    with db() as conn:
        top_rated = conn.execute("""
            SELECT title, author, series, rating
            FROM hc_books WHERE status_id = 3 AND rating >= 4
            ORDER BY rating DESC, title LIMIT 100
        """).fetchall()

        want_to_read = conn.execute("""
            SELECT title, author FROM hc_books WHERE status_id = 1
            ORDER BY title LIMIT 200
        """).fetchall()

        currently_reading = conn.execute("""
            SELECT title, author, series FROM hc_books WHERE status_id = 2 ORDER BY title
        """).fetchall()

        dnf_books = conn.execute("""
            SELECT title, author FROM hc_books WHERE status_id = 4 ORDER BY title
        """).fetchall()

        low_rated = conn.execute("""
            SELECT title, author, series, rating
            FROM hc_books WHERE status_id = 3 AND rating > 0 AND rating <= 2
            ORDER BY rating ASC, title
        """).fetchall()

        existing_recs = conn.execute(
            "SELECT title, author FROM recommendations ORDER BY title"
        ).fetchall()

        passed_with_notes = conn.execute("""
            SELECT r.title, r.author, ri.user_notes
            FROM recommendations r
            JOIN rec_interactions ri ON ri.rec_id = r.id
            WHERE ri.profile_id = ? AND ri.user_status = 'pass' AND ri.user_notes IS NOT NULL
        """, (profile_id,)).fetchall()

        read_recs = conn.execute("""
            SELECT r.title, r.author, ri.user_rating, ri.user_notes
            FROM recommendations r
            JOIN rec_interactions ri ON ri.rec_id = r.id
            WHERE ri.profile_id = ? AND ri.user_status = 'read'
        """, (profile_id,)).fetchall()

    profile = get_profile(profile_id)
    return {
        "profile_id": profile_id,
        "profile_name": profile["name"] if profile else "Unknown",
        "preferences": (profile["preferences"] or "") if profile else "",
        "top_rated_books": [dict(r) for r in top_rated],
        "want_to_read": [dict(r) for r in want_to_read],
        "currently_reading": [dict(r) for r in currently_reading],
        "dnf_books": [dict(r) for r in dnf_books],
        "low_rated_books": [dict(r) for r in low_rated],
        "existing_recs": [dict(r) for r in existing_recs],
        "passed_with_notes": [dict(r) for r in passed_with_notes],
        "read_recs": [dict(r) for r in read_recs],
    }
