# Bookclub — Product Requirements

## Overview

A self-hosted, LAN-only reading companion for a household of two. The app aggregates reading data from external services (Hardcover, Audiobookshelf), maintains a shared recommendation catalog with per-user tracking, and uses Claude Code to generate personalised AI recommendations.

**Non-goals:** public exposure, multi-tenancy at scale, authentication/authorization, mobile-first design.

---

## Personas

**Primary user** — heavy reader, primarily audiobooks via Audiobookshelf, prefers long series.

**Secondary user** — different taste profile from the primary user. Uses the same app on the LAN.

---

## Core Requirements

### 1. Hardcover Integration

- Pull the user's full book shelf from the Hardcover GraphQL API (paginated, handles large libraries)
- Capture: title, author, series, cover URL, status (want-to-read / reading / read / DNF), star rating
- Sync is incremental via upsert — never destructive
- Surface clear errors if the API is unreachable, returns errors, or the token is invalid

### 2. Audiobookshelf Integration

- Cross-reference the ABS SQLite database (mounted read-only) against the rec catalog
- Track: whether a rec is in the ABS library, listen progress (%), finished status
- Use fuzzy title normalisation (unicode normalise → strip articles → difflib) for matching
- Gracefully degrade if the ABS DB is absent or inaccessible

### 3. Recommendation Catalog

- Shared catalog: a recommendation exists once regardless of how many profiles interact with it
- Per-profile interaction row: status (pending / queued / pass / read), rating (1–5), notes
- New recs land as `pending` for every profile
- Recommendations are deduplicated by title on import

### 4. Recommendation Workflow

Each rec moves through a lifecycle:

```
pending → queued → read (+ optional rating)
pending → pass (+ optional note)
pass → pending (undo)
queued → pass (remove)
```

Notes on passed recs are fed back to the AI as a negative signal. Notes on read recs are fed back as positive signal.

### 5. AI Recommendations

**Primary (in-app):** `POST /recs/generate` → Anthropic API → results saved directly to DB
- Context assembled from: preferences, top-rated books (4–5★), currently reading, DNF, passed-with-notes, read-with-ratings, existing titles
- Each rec includes a confidence score (0–100) and genre tags
- Pre-flight and post-response deduplication against existing titles

**Legacy (host-side):** `scripts/refresh_recs.py` calls `claude -p` via subprocess and imports via API
- Requires Claude Code CLI installed on the host
- Still useful for manual runs or when `ANTHROPIC_API_KEY` is not configured in the container
- The app exposes `GET /api/context` and `POST /api/recs/import` for this flow

### 6. Profiles

- Multiple named profiles per household
- Each profile tracks its own queue, statuses, ratings, and notes independently
- Each profile has a free-text preferences field injected into AI prompts
- Active profile stored in a browser cookie (`profile_id`, 1 year TTL)
- Profile switching from any page via nav chip dropdown

### 7. Queue

- Ordered reading list, per-profile
- Drag-and-drop reorder (SortableJS) with server persistence
- Up/down move buttons as fallback
- Position stored as integer in the `queue` table

### 8. Rating Queue

- Surfaces two sets of unrated items for efficient catch-up:
  1. Recs marked read but not yet rated (rec_interactions)
  2. HC books on the Read shelf without a star rating (hc_books)
- Ratings applied here feed directly into the AI context on the next rec generation

### 9. History

- Full Hardcover Read shelf, searchable by title/author
- Filterable by minimum star rating
- Paginated (50 per page) to handle large libraries (1000+ books)

### 10. Sync

- Background sync with a threading lock (prevents concurrent runs)
- Sync log in the DB: start time, finish time, counts, status, error message
- Status indicator in nav: polls every 2s while running, stops when idle
- Timestamp displayed in local time (TZ env var respected)

---

## Data Model

### Tables

| Table | Purpose |
|-------|---------|
| `profiles` | Named profiles with optional preferences text |
| `hc_books` | Mirror of the user's Hardcover library |
| `recommendations` | Shared rec catalog (title, author, series, type, audio, reason, source) |
| `rec_interactions` | Per-profile state for each rec (status, rating, notes) |
| `queue` | Ordered per-profile reading queue |
| `sync_log` | Sync history (timestamps, counts, status, errors) |

### Key design decisions

- `recommendations` and `rec_interactions` are intentionally separate — the catalog is shared, the interaction is personal
- Legacy columns (`user_status`, `user_rating`, `user_notes` on `recommendations`) remain for migration safety; `_REC_COLS` explicitly excludes them to avoid sqlite3.Row name collisions
- `_REC_COLS` / `_REC_JOINS` constants centralise the profile-aware rec query to avoid repetition and the column-collision bug
- `_now()` helper uses `datetime.now()` (not SQLite `CURRENT_TIMESTAMP`) to respect the `TZ` env var

---

## Tech Stack

| Layer | Choice | Reason |
|-------|--------|--------|
| Web framework | FastAPI | Async-capable, Pydantic, good HTMX story |
| Templates | Jinja2 | Server-rendered, no JS build step |
| Interactivity | HTMX | Partial updates without a SPA; keeps the stack simple |
| Drag-and-drop | SortableJS | Drop-in, no framework needed |
| Database | SQLite (WAL mode) | Zero dependencies, sufficient for household scale |
| HTTP client | httpx | Async-capable, used for Hardcover API calls |
| Container | Docker (python:3.11-slim) | Consistent runtime, named volumes for data persistence |
| AI | Anthropic API (`claude-sonnet-4-6`) | In-container generation via SDK; legacy host-side script also available |

---

## Infrastructure

- Runs in Docker (tested on Windows + Docker Desktop/WSL2, and Linux)
- Named volume `bookclub_data` for the SQLite database
- Named volume `audiobookshelf_config` mounted read-only for ABS DB access
- Port configured via `.env` (`BOOKCLUB_PORT`)
- `TZ` env var controls all displayed timestamps
- Watchtower auto-update disabled (manual updates only)
- `scripts/refresh_recs.py` runs on the host (outside Docker) — requires Claude Code CLI installed

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Recommendations page (filter via `?status=`) |
| POST | `/rec/{id}/queue` | Add rec to queue |
| POST | `/rec/{id}/pass` | Pass on rec |
| POST | `/rec/{id}/unpass` | Undo pass |
| POST | `/rec/{id}/read` | Mark as read |
| POST | `/rec/{id}/rate` | Rate rec (form: `rating` 1–5) |
| POST | `/rec/{id}/note` | Save notes |
| GET | `/queue` | Queue page |
| POST | `/queue/{id}/remove` | Remove from queue |
| POST | `/queue/{id}/move/{direction}` | Move up/down |
| POST | `/queue/reorder` | Bulk reorder (SortableJS) |
| GET | `/history` | Read history (params: `q`, `rating`, `page`) |
| GET | `/rate` | Rating queue |
| POST | `/hc/{id}/rate` | Rate a Hardcover book |
| GET | `/profiles` | Profiles management |
| POST | `/profiles` | Create profile |
| POST | `/profiles/switch/{id}` | Switch active profile |
| POST | `/profiles/{id}/preferences` | Save preferences |
| GET | `/recs/refresh` | Refresh instructions page |
| GET | `/api/context` | AI context payload (used by refresh_recs.py) |
| POST | `/api/recs/import` | Import recommendations from JSON |
| POST | `/sync` | Trigger background sync |
| GET | `/sync/status` | Sync status (used by HTMX polling) |

---

## Constraints & Principles

- **LAN-only** — no auth, no HTTPS required, no rate limiting
- **No data loss** — sync is always upsert; user interactions are never overwritten by sync
- **Degrade gracefully** — ABS absent = skip silently; Hardcover error = log and surface, don't crash
- **Timezone-aware** — all timestamps written via `_now()`, never SQLite `CURRENT_TIMESTAMP`
- **Minimal JS** — HTMX handles interactivity; SortableJS for drag-drop; no build toolchain
- **Simple over clever** — pagination at 50 rows, not infinite scroll; SQLite, not Postgres

---

## Future Considerations

These were identified during review but deferred as out-of-scope for current use:

- **Cover art for unlinked recs** — currently relies on Hardcover match or series lookup. Could add Open Library / Google Books fallback via ISBN or title search.
- **Deleting profiles** — currently no delete UI. Would need to handle cascade deletion of queue + interactions.
- **Bulk rec management** — no "pass all" or "clear pending" operations. Manageable at current scale.
- **Hardcover webhook / live sync** — currently sync is manual. A webhook or scheduled cron would keep data fresher.
- **Mobile layout** — cards work acceptably on mobile but the nav is cramped. A hamburger menu would help if the app sees phone use.
- **Export / backup** — the SQLite DB is on a named Docker volume; a backup script or `/export` endpoint would reduce recovery risk.
