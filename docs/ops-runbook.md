# Bookclub — Ops Runbook

Day-to-day operations, troubleshooting, and maintenance procedures.

---

## Starting / Stopping

```bash
# Start
cd /path/to/bookclub
docker compose up -d

# Stop
docker compose down

# Restart (e.g. after config change)
docker compose restart bookclub

# Rebuild after code changes
docker compose up -d --build
```

---

## Logs

```bash
# Follow live logs
docker logs -f bookclub

# Last 100 lines
docker logs --tail 100 bookclub
```

---

## Generating Recommendations

**In-app (primary):** Click **↺ Recs** in the nav. Requires `ANTHROPIC_API_KEY` set in `.env`.

**Legacy script (alternative):**
```bash
# Standard run (10 recs for profile 1)
python3 /path/to/bookclub/scripts/refresh_recs.py

# For a specific profile
python3 /path/to/bookclub/scripts/refresh_recs.py --profile 2

# More recs
python3 /path/to/bookclub/scripts/refresh_recs.py --count 20

# Inspect the prompt without calling Claude
python3 /path/to/bookclub/scripts/refresh_recs.py --dry-run
```

Requires: Claude Code CLI installed on the host (`claude` in PATH).

---

## Database

The SQLite database lives in the `bookclub_data` Docker named volume.

```bash
# Find the volume path on disk
docker volume inspect bookclub_data

# Open an interactive shell in the container to query the DB
docker exec -it bookclub sqlite3 /data/bookclub.db

# Useful queries
sqlite3 /path/to/bookclub.db "SELECT COUNT(*) FROM recommendations;"
sqlite3 /path/to/bookclub.db "SELECT * FROM sync_log ORDER BY id DESC LIMIT 5;"
sqlite3 /path/to/bookclub.db "SELECT * FROM profiles;"
```

### Backup

```bash
# Copy the DB out of the volume
docker cp bookclub:/data/bookclub.db ./bookclub-backup-$(date +%Y%m%d).db
```

### Restore

```bash
# Stop the container first
docker compose down

# Copy backup in
docker run --rm -v bookclub_data:/data -v $(pwd):/backup alpine \
  cp /backup/bookclub-backup-YYYYMMDD.db /data/bookclub.db

# Restart
docker compose up -d
```

---

## Hardcover Token Rotation

1. Go to Hardcover → Settings → API → generate a new token
2. Update `.env`: `HARDCOVER_TOKEN=eyJ...new...token`
3. Restart the container: `docker compose restart bookclub`
4. Trigger a sync from the UI to verify the token works

---

## Troubleshooting

### Sync shows "✗ Error" in the nav

Check the sync log:
```bash
docker exec -it bookclub sqlite3 /data/bookclub.db \
  "SELECT status, message, finished_at FROM sync_log ORDER BY id DESC LIMIT 3;"
```

Common causes:
- `Hardcover API error` — token expired or rate limited; rotate the token
- `Hardcover API returned no user data` — token is valid but wrong user; check Hardcover settings
- `Connection refused` — Hardcover API unreachable; check internet connectivity from the container

### Recommendations not showing cover art

Cover art requires a rec to be fuzzy-matched to a Hardcover book. Run a sync:
1. Click **⟳ Sync** in the nav
2. Wait for it to complete
3. Reload the page

Books you've never added to Hardcover won't match. Series where you haven't read any books yet also won't have covers until you read book 1 and it appears in Hardcover.

### ABS "In Library" badges not appearing

The ABS sync requires:
1. The `audiobookshelf_config` Docker volume to be mounted (Audiobookshelf must have run at least once)
2. The ABS DB file to exist at `/abs_config/absdatabase.sqlite`

Check from inside the container:
```bash
docker exec -it bookclub ls -la /abs_config/absdatabase.sqlite
```

If the file is missing, Audiobookshelf may not have started yet, or the volume name is wrong in `docker-compose.yml`.

### `scripts/refresh_recs.py` fails: "claude not found in PATH"

Install Claude Code on the host:
```bash
npm install -g @anthropic-ai/claude-code
```

Then verify: `claude --version`

### `scripts/refresh_recs.py` fails: "could not reach the app"

The script defaults to `http://localhost:8585`. If the app is on a different port:
```bash
python3 scripts/refresh_recs.py --base-url http://localhost:YOUR_PORT
```

Also verify the container is running: `docker ps | grep bookclub`

### Profile cookie stuck on wrong profile

Clear the `profile_id` cookie in your browser, or navigate to `/profiles` and switch manually.

---

## Updating the App

Watchtower auto-updates are disabled. To update manually after code changes:

```bash
cd /path/to/bookclub
docker compose up -d --build
```

The database is on a named volume and is not affected by rebuilds.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HARDCOVER_TOKEN` | Yes | — | Hardcover API bearer token |
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key for in-app recommendation generation |
| `ANTHROPIC_MODEL` | No | `claude-sonnet-4-6` | Claude model used for generation |
| `ABS_URL` | No | — | Audiobookshelf API base URL (e.g. `http://192.168.144.1:13378`) |
| `ABS_TOKEN` | No | — | Audiobookshelf API bearer token |
| `ABS_PLAYLIST_ID` | No | — | Default ABS picks playlist ID |
| `BOOKCLUB_PORT` | No | `8585` | Host port to bind |
| `TZ` | No | `UTC` | Timezone for displayed timestamps |
| `DB_PATH` | No | `/data/bookclub.db` | SQLite DB path inside container |
| `ABS_DB_PATH` | No | `/abs_config/absdatabase.sqlite` | ABS DB path inside container |

---

## Known Gotchas

### ABS inter-container networking
Bookclub cannot reach ABS by container name (different Docker networks). Use the Docker host gateway IP `192.168.144.1` instead (discoverable via `/proc/net/route` inside the container). ABS API is at `192.168.144.1:13378`.

### ABS cover images
Served via a proxy endpoint (`GET /abs/cover/{item_id}`) — fetches from ABS API with Bearer token and caches 86400s. The browser can't reach ABS directly, and the cover path is inside the ABS container's filesystem.

### ABS playlist PATCH is broken
`PATCH /api/playlists/{id}` with `{"items": [...]}` always returns `400 Invalid playlist items. Length mismatch` even for valid library item IDs. Workaround: DELETE the playlist and POST a new one with items in the creation body — this works reliably. Update the cached playlist ID after each rebuild. Playlist item format: `{"libraryItemId": "...", "episodeId": null}`.

### SQL reserved word `order`
ABS SQLite schema uses `order` as a column name in `playlistMediaItems`. Must quote it as `pmi."order"` in queries.

### HTMX polling — browser tab spinner
HTMX uses XHR which triggers the browser's native loading indicator on every poll. For background polling (sync log panel), use plain `fetch()` in JS instead. Pattern: `setInterval(() => fetch('/endpoint').then(r => r.json()).then(updateUI), 2000)`, clear interval when done.

### HTMX `hx-swap="outerHTML"` + `hx-on` events
After an element replaces itself via outerHTML, `hx-on` event handlers on that element don't fire reliably (old element is gone). Use a document-level event listener instead.

### Claude API duplicate recommendations
When asking Claude for a list with "do NOT repeat" constraints, it sometimes includes a duplicate and puts self-correction text in the `reason` field. Fix: (1) prompt explicitly says to silently skip duplicates and never put meta-commentary in `reason`; (2) deduplicate the returned list in code against existing DB titles before upserting.

### `_sync_running` race condition
Setting `_sync_running = True` inside the background thread means the flag isn't set when the POST handler returns — the UI sees `running=False` immediately after POST. Fix: set `_sync_running = True` in the route handler before scheduling the background task.

### Hardcover rate limiting
HC API returns 429 if synced too rapidly. Don't trigger multiple syncs in quick succession during development.

### `recommendations` table is shared (not per-profile)
All AI and ABS-playlist recs live in `recommendations` globally. Per-profile interaction state lives in `rec_interactions`. Per-profile ordering lives in `queue`. "Clearing a user's recs" means deleting their `queue` + `rec_interactions` rows — do NOT delete from `recommendations` (that would wipe all users' data). "Deleting a user" means deleting their `queue` + `rec_interactions` + `profiles` row only.
