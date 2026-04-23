# Bookclub

A self-hosted book recommendation app for households with shared — or wildly different — reading tastes.

Syncs your reading history from [Hardcover](https://hardcover.app), cross-references it with your [Audiobookshelf](https://www.audiobookshelf.org) library, and uses the [Claude AI API](https://www.anthropic.com) to generate personalized recommendations for each person in your household. Each user gets their own queue, ratings, and notes — recommendations are shared but interactions are independent.

---

## Features

**AI Recommendations**
- Generates book and series suggestions via the Anthropic API, tailored per profile
- Confidence scores (0–100) showing how well each recommendation fits your taste
- Context sent to Claude includes: your stated preferences, top-rated books, currently reading, DNF list, passed recs with notes, and all existing titles for deduplication
- Filter tabs: All · Pending · Queued · In Library · Pass · Read

**Hardcover Integration**
- Pulls your full bookshelf (want-to-read, currently reading, read, DNF) via the Hardcover GraphQL API
- Paginated — handles large libraries (1,000+ books)
- Ratings sync as positive/negative signals into AI context

**Audiobookshelf Integration**
- Reads the ABS SQLite database directly (mounted read-only) to identify which recommendations you already own
- Tracks listen progress and finished status per recommendation
- **Bookclub Picks playlist** — auto-synced ABS playlist of AI-recommended audiobooks you own, ordered by confidence score; rebuilt on every sync

**Per-Profile Support**
- Multiple named profiles (designed for a household of two, works for more)
- Each profile has its own queue, statuses, ratings, notes, and ABS token
- Profile switching from any page via the nav chip
- Free-text preferences field per profile — highest-signal input for recommendation quality

**Other**
- Reading queue with drag-and-drop reorder (SortableJS)
- Rating queue to catch up on unrated Hardcover books and read recommendations
- Full read history with search and rating filter, paginated at 50 per page
- Real-time sync log panel (pure `fetch()` polling — no browser spinner)
- SQLite in WAL mode — reads never block writes

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| Web framework | FastAPI |
| Templates | Jinja2 — server-rendered, no build step |
| Interactivity | HTMX + SortableJS |
| Database | SQLite (WAL mode) |
| Container | Docker (`python:3.11-slim`) |
| AI | Anthropic API (`claude-sonnet-4-6` by default) |
| External book data | Hardcover GraphQL API |
| Cover art | Hardcover (primary) · Open Library API (fallback) |

---

## Prerequisites

- Docker + Docker Compose
- A [Hardcover](https://hardcover.app) account with some reading history
- An [Anthropic API key](https://console.anthropic.com) for recommendation generation
- (Optional) [Audiobookshelf](https://www.audiobookshelf.org) for library cross-reference and Bookclub Picks playlists

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/nickwolf/bookclub.git
cd bookclub

# 2. Configure environment
cp .env.example .env
# Edit .env — at minimum set HARDCOVER_TOKEN and ANTHROPIC_API_KEY

# 3. Build and start
docker compose up -d --build

# 4. Open http://localhost:8585
```

Then click **⟳ Sync** to pull in your Hardcover data, and **↺ Recs** to generate your first AI recommendations.

### With Audiobookshelf

Set `ABS_URL` and `ABS_TOKEN` in `.env`. Then edit the `audiobookshelf_config` volume in `docker-compose.yml` to point at your existing ABS volume (instructions are in the comments at the bottom of the file). Make sure the volume name matches what your ABS container uses.

For the `ABS_URL`, use your Docker bridge gateway IP (e.g. `http://192.168.x.x:13378`), not `localhost` — containers can't reach each other via localhost. See the [ops runbook](docs/ops-runbook.md) for details.

---

## Environment Variables

See [`.env.example`](.env.example) for the full list with descriptions.

**Required:**

| Variable | Where to find it |
|----------|-----------------|
| `HARDCOVER_TOKEN` | Hardcover → Settings → API |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |

**Optional (Audiobookshelf):**

| Variable | Description |
|----------|-------------|
| `ABS_URL` | ABS API base URL (e.g. `http://192.168.x.x:13378`) |
| `ABS_TOKEN` | ABS API token — Settings → Users → your account |

**Other:**

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Claude model for generation |
| `BOOKCLUB_PORT` | `8585` | Host port for the web UI |
| `TZ` | `UTC` | Timezone for displayed timestamps |

---

## How It Works

### Recommendation context

Every time you generate recommendations, the app sends Claude a structured prompt containing:

1. Your stated preferences (free-text, injected first as highest-signal)
2. Up to 60 of your highest-rated Hardcover books (4–5★)
3. Books you're currently reading
4. Books you DNF'd
5. Passed recommendations with your notes (negative signal)
6. Read recommendations with ratings and notes
7. All existing recommendation titles (so Claude doesn't repeat them)

Claude returns a JSON array. Responses are deduplicated against existing titles at the code level as well, and each rec includes a confidence score (0–100) and genre tags.

### ABS library sync

Bookclub mounts the Audiobookshelf config volume read-only and queries its SQLite database directly — no ABS API required for the library cross-reference. The ABS API is only used for writing the Bookclub Picks playlist.

### Fuzzy title matching

Recommendations are linked to Hardcover and ABS entries using two-stage matching:
1. Unicode normalize → strip articles (the/a/an) → collapse whitespace → lowercase
2. `difflib.get_close_matches` (cutoff 0.8)
3. A 4-word prefix check handles ABS titles truncated at subtitles

### Sync log panel

The **⟳ Sync** button fires a background task and immediately opens a slide-in log panel. The panel polls `/sync/log-entries` every 2 seconds via plain `fetch()` (not HTMX) so the browser's native loading indicator never activates. Polling stops automatically when the sync finishes.

---

## Documentation

- [Architecture](docs/architecture.md) — directory structure, request flow, full DB schema, key patterns
- [Ops Runbook](docs/ops-runbook.md) — start/stop, logs, DB backup/restore, troubleshooting
- [User Guide](docs/user-guide.md) — how to use the app day-to-day
- [Product Requirements](docs/product-requirements.md) — feature spec and design decisions

---

## License

[MIT](LICENSE)
