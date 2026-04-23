# Bookclub — Architecture

## Directory Structure

```
bookclub/
├── Dockerfile
├── docker-compose.yml
├── .env                        # HARDCOVER_TOKEN, BOOKCLUB_PORT, TZ (not in git)
├── .env.example
├── .gitignore
├── scripts/
│   └── refresh_recs.py         # Legacy host-side AI rec generation script
├── docs/
│   ├── user-guide.md
│   ├── product-requirements.md
│   └── architecture.md         # this file
└── app/
    ├── main.py                 # FastAPI routes
    ├── db.py                   # Database layer
    ├── sync.py                 # Hardcover + ABS sync logic
    ├── gen.py                  # In-container AI rec generation (Anthropic API)
    ├── static/
    │   └── style.css
    └── templates/
        ├── base.html
        ├── recommendations.html
        ├── queue.html
        ├── rating_queue.html
        ├── history.html
        ├── profiles.html
        ├── recs_refresh.html
        └── partials/
            ├── rec_card.html
            ├── queue_list.html
            └── sync_status.html
```

---

## Request Flow

```
Browser
  │
  ├── GET /                        → main.py → db.get_recommendations() → recommendations.html
  ├── POST /rec/{id}/queue         → main.py → db.add_to_queue() → rec_card.html (HTMX swap)
  ├── POST /sync                   → main.py → sync.run_full_sync() [background thread]
  │                                                ├── sync.sync_hardcover() → Hardcover GraphQL API
  │                                                ├── sync.sync_abs()       → ABS SQLite (read-only)
  │                                                ├── sync.sync_abs_picks() → ABS API (playlist)
  │                                                └── sync.link_recs_to_hc()
  │
  ├── POST /recs/generate          → main.py → gen.run_generation() [background thread]
  │                                                ├── db.get_rec_context()
  │                                                ├── gen.build_prompt()
  │                                                ├── Anthropic API (ANTHROPIC_MODEL)
  │                                                ├── db.upsert_recommendation()
  │                                                └── gen._fetch_covers_sync() → Open Library API
  │
  ├── GET /recs/generate/status    → main.py → _gen_running / _gen_last (HTMX polling)
  │
  └── GET /api/context             → db.get_rec_context()
        ↑                                             ↑
Host (outside Docker)                            scripts/refresh_recs.py  (legacy — prefer in-app generation)
  └── python3 scripts/refresh_recs.py
        ├── GET /api/context          (fetch context from app)
        ├── claude -p {prompt}        (call Claude Code CLI)
        └── POST /api/recs/import     (import results back)
```

---

## Database Schema

```sql
profiles (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  name                  TEXT NOT NULL UNIQUE,
  preferences           TEXT,                    -- free-text; injected into AI prompts
  abs_token             TEXT,                    -- per-profile ABS token override (falls back to ABS_TOKEN env)
  abs_picks_playlist_id TEXT,                    -- per-profile picks playlist override (falls back to ABS_PLAYLIST_ID env)
  created_at            DATETIME
)

hc_books (
  id          INTEGER PRIMARY KEY,     -- Hardcover book ID
  title       TEXT NOT NULL,
  author      TEXT,
  series      TEXT,
  series_pos  REAL,
  cover_url   TEXT,
  status_id   INTEGER,                 -- 1=Want, 2=Reading, 3=Read, 4=DNF
  rating      REAL,
  synced_at   DATETIME
)

recommendations (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  hc_book_id          INTEGER REFERENCES hc_books(id),  -- set by link_recs_to_hc()
  title               TEXT NOT NULL,
  author              TEXT,
  series              TEXT,
  type                TEXT,            -- "Book" or "Series"
  audiobook_available TEXT,            -- "Yes", "No", "Partial", "Unknown"
  in_abs_library      INTEGER,         -- 0/1, updated by sync_abs()
  abs_progress        REAL,            -- 0.0–1.0, updated by sync_abs()
  abs_finished        INTEGER,         -- 0/1
  abs_library_item_id TEXT,            -- ABS item ID when matched to library
  abs_description     TEXT,            -- ABS item description
  abs_duration        REAL,            -- audiobook duration in seconds
  abs_narrator        TEXT,
  abs_genres          TEXT,            -- comma-separated genres from ABS
  abs_series_seq      TEXT,            -- series sequence string from ABS
  reason              TEXT,            -- AI-generated explanation
  tags                TEXT,            -- comma-separated genre/theme tags from AI
  confidence          REAL,            -- 0–100, AI-generated fit score
  cover_url           TEXT,            -- Open Library cover (fallback; HC cover takes priority in queries)
  source              TEXT,            -- "claude-api", "claude-cli", "seed", etc.
  created_at          DATETIME,
  updated_at          DATETIME,
  -- Legacy columns (pre-profiles refactor, retained for migration safety):
  user_status TEXT, user_rating INTEGER, user_notes TEXT
)

rec_interactions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  INTEGER NOT NULL REFERENCES profiles(id),
  rec_id      INTEGER NOT NULL REFERENCES recommendations(id),
  user_status TEXT DEFAULT 'pending',  -- pending/queued/pass/read
  user_rating INTEGER,                 -- 1–5
  user_notes  TEXT,
  updated_at  DATETIME,
  UNIQUE(profile_id, rec_id)
)

queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id  INTEGER NOT NULL REFERENCES profiles(id),
  rec_id      INTEGER NOT NULL REFERENCES recommendations(id),
  position    INTEGER NOT NULL,
  added_at    DATETIME
)

sync_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  DATETIME,
  finished_at DATETIME,
  hc_synced   INTEGER,
  abs_synced  INTEGER,
  status      TEXT,     -- "running", "ok", "error"
  message     TEXT
)
```

---

## Key Patterns

### Profile-aware queries

All recommendation queries use `_REC_COLS` and `_REC_JOINS` constants in `db.py`. These:
- Join `rec_interactions` on `profile_id = :pid`
- Use `COALESCE(ri.user_status, 'pending')` so recs without an interaction row appear as pending
- Join `hc_books` for `cover_url`, with a series-fallback subquery
- Exclude legacy columns from `recommendations` to avoid `sqlite3.Row` name collisions

```python
# db.py
_REC_COLS = """
    r.id, r.hc_book_id, r.title, r.author, r.series, r.type,
    r.audiobook_available, r.in_abs_library, r.abs_progress, r.abs_finished,
    r.reason, r.source, r.created_at, r.updated_at,
    COALESCE(ri.user_status, 'pending') AS user_status,
    ri.user_rating, ri.user_notes,
    q.id AS queue_id, q.position AS queue_pos,
    COALESCE(
        h.cover_url,
        (SELECT h2.cover_url FROM hc_books h2
         WHERE r.series IS NOT NULL AND r.series != ''
           AND h2.series = r.series AND h2.series_pos > 0
         ORDER BY h2.series_pos ASC LIMIT 1)
    ) AS cover_url
"""
_REC_JOINS = """
    FROM recommendations r
    LEFT JOIN rec_interactions ri ON ri.rec_id = r.id AND ri.profile_id = :pid
    LEFT JOIN queue q             ON q.rec_id  = r.id AND q.profile_id  = :pid
    LEFT JOIN hc_books h          ON h.id = r.hc_book_id
"""
```

### Timestamps

All explicit timestamp writes use `_now()`:
```python
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
```
This respects the `TZ` environment variable. SQLite's `CURRENT_TIMESTAMP` is always UTC and is only used for `DEFAULT` values on columns where the app doesn't set an explicit time.

### HTMX partial updates

Actions on rec cards (`/rec/{id}/queue`, `/pass`, etc.) return `rec_card.html` rendered for that single rec, swapped via `hx-swap="outerHTML"`. This keeps all card state in sync without a full page reload. The `_card()` helper in `main.py` centralises this pattern.

### Background sync

```python
_sync_lock = threading.Lock()
_sync_running = False
```

The lock prevents concurrent syncs. The boolean `_sync_running` is read by `GET /sync/status` for the HTMX polling indicator. The sync itself runs in a `BackgroundTasks` callback so the POST returns immediately.

### Fuzzy title matching

Two-stage matching in `sync.py`:
1. Unicode normalise → strip articles (the/a/an) → collapse whitespace → lowercase
2. `difflib.get_close_matches(norm, candidates, cutoff=0.8)` for rec→HC linking (stricter)
3. Shortened 4-word prefix also checked for ABS matching (handles subtitle truncation)

### AI context flow

**Primary (in-container):** `POST /recs/generate` → `gen.build_prompt()` → Anthropic API → `gen.extract_json()` → `db.upsert_recommendation()` → Open Library cover fetch

`gen.py` reads context fresh from the DB on each run via `db.get_rec_context()`. Recs include `confidence` (0–100 fit score) and `tags` (genre/theme list). Open Library covers are fetched synchronously as a best-effort fallback; HC covers take priority in all queries via `COALESCE`.

**Legacy (WSL2 host):** `refresh_recs.py` → `GET /api/context` → `claude -p {prompt}` → `POST /api/recs/import`

The script still works for manual runs or when `ANTHROPIC_API_KEY` is not set in the container. `--dry-run` prints the full prompt without calling Claude.

---

## Deployment

```bash
# First run (create the named volume if it doesn't exist)
docker volume create bookclub_data

# Build and start
cd /path/to/bookclub
docker compose up -d --build

# View logs
docker logs -f bookclub

# Rebuild after code changes
docker compose up -d --build
```

The `audiobookshelf_config` volume is created by the Audiobookshelf container and shared here read-only — ensure Audiobookshelf is running before bookclub starts, or the ABS sync step will silently skip.

---

## Adding Recommendations Manually

Via the API (from WSL2 host or any LAN machine):

```bash
curl -X POST http://localhost:8585/api/recs/import \
  -H 'Content-Type: application/json' \
  -d '{
    "profile_id": 1,
    "recs": [
      {
        "title": "Book Title",
        "author": "Author Name",
        "series": "Series Name or null",
        "type": "Book",
        "audiobook_available": "Yes",
        "reason": "Why this fits the profile."
      }
    ]
  }'
```

---

## Hardcover API Notes

- Endpoint: `https://api.hardcover.app/v1/graphql`
- Auth: Bearer token in `Authorization` header (`HARDCOVER_TOKEN` env var)
- Token location: Hardcover → Settings → API
- Paginated: 100 books per request, loop until response length < limit
- Status IDs: `1`=Want to Read, `2`=Reading, `3`=Read, `4`=DNF
- Author field path: `contributions[].author.name` (not `authors[]`)
- Cover URL path: `image.url`

## Audiobookshelf DB Notes

- Path: `/abs_config/absdatabase.sqlite` (mounted as `audiobookshelf_config:/abs_config:ro`)
- Relevant tables: `libraryItems`, `books`, `mediaProgresses`
- Progress join: `json_extract(mp.extraData, '$.libraryItemId') = li.id`
- Progress percentage: `currentTime / duration`
