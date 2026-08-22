# Scheduled Sync & ntfy Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app re-syncs itself on an interval (no more manual-only ⟳), and pushes ntfy notifications when a queued book newly appears in the ABS library, when generation finishes, and when a sync fails.

**Architecture:** A daemon scheduler thread in `main.py` wakes every 15 minutes and triggers `_run_sync()` when the last successful sync is older than `SYNC_INTERVAL_HOURS` (default 6; `0` disables). A tiny `notify.py` module POSTs to a ntfy topic URL (`NTFY_URL`, optional `NTFY_TOKEN`) and is a silent no-op when unconfigured. `sync_abs()` reports which recs newly flipped `in_abs_library` 0→1; `run_full_sync()` cross-references those against the queue and notifies.

**Tech Stack:** `threading` (stdlib), `httpx` (already installed), ntfy HTTP API (plain POST, Title/Tags/Priority headers).

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub`; test harness from Plan 01 (`test_db`, `client` fixtures); run `.venv/bin/pytest -q` from repo root.
- No new pip dependencies. `app/` code hot-reloads; the compose/env changes in Task 4 need `docker compose up -d` (fine from WSL2 for this project).
- All notifications must be **best-effort**: a ntfy failure logs a warning via `db.log` and never breaks sync or generation.
- Existing sync concurrency rules stand: `_sync_lock` prevents concurrent syncs; `_sync_running` is set in the route/scheduler path, not inside the thread (see ops-runbook "Known Gotchas").
- Timestamps via `db._now()`; read ops-runbook Known Gotchas before touching sync.
- Commits: conventional, no AI-attribution lines.

---

### Task 1: `notify.py` module

**Files:**
- Create: `app/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `notify.notify(title: str, message: str, tags: str = "books", priority: str = "3") -> bool` — returns False (no-op) when `notify.NTFY_URL` is empty; True after a successful POST; False + warning log on exception. Module constants `NTFY_URL`, `NTFY_TOKEN` read from env at import (monkeypatchable in tests).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notify.py`:

```python
def test_notify_noop_when_unconfigured(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "NTFY_URL", "")
    assert notify.notify("t", "m") is False


def test_notify_posts_title_and_body(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "NTFY_URL", "https://ntfy.example/bookclub")
    monkeypatch.setattr(notify, "NTFY_TOKEN", "tok123")
    calls = {}

    class FakeClient:
        def __init__(self, timeout): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, content=None, headers=None):
            calls.update(url=url, content=content, headers=headers)

    monkeypatch.setattr(notify.httpx, "Client", FakeClient)
    assert notify.notify("New book", "Dune arrived", tags="tada") is True
    assert calls["url"] == "https://ntfy.example/bookclub"
    assert calls["content"] == b"Dune arrived"
    assert calls["headers"]["Title"] == "New book"
    assert calls["headers"]["Tags"] == "tada"
    assert calls["headers"]["Authorization"] == "Bearer tok123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'notify'`

- [ ] **Step 3: Implement `app/notify.py`**

```python
"""Best-effort push notifications via ntfy. No-op when NTFY_URL is unset."""
import os

import httpx

import db

NTFY_URL   = os.environ.get("NTFY_URL", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")


def notify(title: str, message: str, tags: str = "books", priority: str = "3") -> bool:
    if not NTFY_URL:
        return False
    headers = {"Title": title, "Tags": tags, "Priority": priority}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        with httpx.Client(timeout=5) as client:
            client.post(NTFY_URL, content=message.encode(), headers=headers)
        return True
    except Exception as e:
        db.log("notify", f"ntfy send failed: {e}", level="warning")
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_notify.py -v` — 2 PASS

- [ ] **Step 5: Commit**

```bash
git add app/notify.py tests/test_notify.py
git commit -m "feat: ntfy notification helper (best-effort, env-gated)"
```

---

### Task 2: "Queued book arrived in library" detection

**Files:**
- Modify: `app/sync.py` (`sync_abs` line ~272, `run_full_sync` lines ~556-594; add `import notify` at top)
- Test: `tests/test_sync_notifications.py`

**Interfaces:**
- Consumes: `notify.notify` from Task 1.
- Produces: `sync.sync_abs(rec_rows) -> tuple[int, list[tuple[int, str]]]` — now returns `(updated_count, newly_available)` where `newly_available` is `[(rec_id, title), ...]` for recs whose `in_abs_library` flipped 0→1 this run. `run_full_sync` notifies for the subset of those that sit in any profile's queue. **Breaking change to sync_abs's return — the only caller is `run_full_sync`, updated here.**

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_notifications.py`:

```python
def test_sync_abs_reports_newly_available(test_db, monkeypatch):
    import sync

    rec_id = test_db.upsert_recommendation(
        "The Blade Itself", "Joe Abercrombie", None, "Book", "Yes", "grimdark")

    # Fake ABS DB contents: the book is now in the library
    def fake_read_abs_db():
        titles = [sync._norm("The Blade Itself")]
        return titles, {}, {sync._norm("The Blade Itself"): "li_123"}, {"li_123": {}}
    monkeypatch.setattr(sync, "_read_abs_db", fake_read_abs_db)

    with test_db.db() as conn:
        recs = conn.execute(
            "SELECT id, title, hc_book_id, abs_library_item_id, in_abs_library "
            "FROM recommendations").fetchall()

    updated, newly = sync.sync_abs(recs)
    assert updated == 1
    assert newly == [(rec_id, "The Blade Itself")]

    # Second run: already in library, so nothing is "newly" available
    with test_db.db() as conn:
        recs = conn.execute(
            "SELECT id, title, hc_book_id, abs_library_item_id, in_abs_library "
            "FROM recommendations").fetchall()
    _, newly2 = sync.sync_abs(recs)
    assert newly2 == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_notifications.py -v`
Expected: FAIL — `sync_abs` returns an `int`, cannot unpack.

- [ ] **Step 3: Implement in `app/sync.py`**

Add `import notify` with the other imports at the top of `sync.py`.

Rewrite `sync_abs` (line ~272):

```python
def sync_abs(rec_rows: list) -> tuple[int, list[tuple[int, str]]]:
    """Cross-reference all recommendations against the ABS DB.

    Returns (count_updated, newly_available) where newly_available lists
    (rec_id, title) for recs that just flipped in_abs_library 0 -> 1.
    """
    library_titles, progress_map, item_id_map, book_details = _read_abs_db()
    if not library_titles:
        return 0, []

    updated = 0
    newly_available: list[tuple[int, str]] = []
    for rec in rec_rows:
        in_lib = _fuzzy_match(rec["title"], library_titles)
        if in_lib and not rec["in_abs_library"]:
            newly_available.append((rec["id"], rec["title"]))
        norm = _norm(rec["title"])
        prog_entry = progress_map.get(norm)
        progress = prog_entry[0] if prog_entry else None
        finished = prog_entry[1] if prog_entry else False

        db.update_rec_abs_status(rec["id"], in_lib, progress, finished)

        # Store rich ABS data and library item ID
        if in_lib:
            matches = difflib.get_close_matches(norm, list(item_id_map.keys()), n=1, cutoff=0.72)
            if matches:
                lib_id = item_id_map[matches[0]]
                details = book_details.get(lib_id, {})
                db.update_rec_abs_data(
                    rec["id"],
                    library_item_id=lib_id,
                    description=details.get("description"),
                    duration=details.get("duration"),
                    narrator=details.get("narrator"),
                    genres=details.get("genres"),
                    series=details.get("series"),
                    series_seq=details.get("series_seq"),
                    cover_url=f"/abs/cover/{lib_id}",
                )

        updated += 1

    return updated, newly_available
```

In `run_full_sync`, update the recs query (line ~563) to include the flag, unpack the new return, and notify:

```python
        with db.db() as conn:
            recs = conn.execute(
                "SELECT id, title, hc_book_id, abs_library_item_id, in_abs_library "
                "FROM recommendations"
            ).fetchall()

        abs_count, newly_available = sync_abs(recs)
        db.log("sync", f"ABS library sync complete — {abs_count} recommendations cross-referenced")

        if newly_available:
            ids = [rec_id for rec_id, _ in newly_available]
            placeholders = ",".join("?" * len(ids))
            with db.db() as conn:
                queued_hits = conn.execute(f"""
                    SELECT DISTINCT r.title, p.name
                    FROM queue q
                    JOIN recommendations r ON r.id = q.rec_id
                    JOIN profiles p ON p.id = q.profile_id
                    WHERE q.rec_id IN ({placeholders})
                """, ids).fetchall()
            for row in queued_hits:
                notify.notify(
                    "📚 New in your library",
                    f"“{row['title']}” is now in Audiobookshelf — it's in {row['name']}'s queue.",
                    tags="tada,books",
                )
```

Also add a failure notification in the `except` block of `run_full_sync` (after the existing `db.log(...)` error line):

```python
        notify.notify("Bookclub sync failed", str(e), tags="warning", priority="4")
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (including the new test).

- [ ] **Step 5: Commit**

```bash
git add app/sync.py tests/test_sync_notifications.py
git commit -m "feat: notify when a queued book newly appears in the ABS library"
```

---

### Task 3: Interval scheduler + generation-complete notification

**Files:**
- Modify: `app/main.py` (`startup` lines 41-59, `_run_gen` lines 27-38; add `import time` and `import notify`)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: existing `_run_sync`, `_sync_lock`; `db.get_last_sync()`; `notify.notify`.
- Produces: `main.SYNC_INTERVAL_HOURS: float` (env, default 6.0; 0 disables), `main._should_auto_sync() -> bool` (now interval-aware), `main._sync_scheduler()` daemon loop (checks every 15 min).

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler.py`:

```python
def test_should_auto_sync_respects_interval(test_db, monkeypatch):
    import main
    from datetime import datetime, timedelta

    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 6.0)

    # No sync ever -> True
    assert main._should_auto_sync() is True

    # Fresh successful sync -> False
    log_id = test_db.start_sync_log()
    test_db.finish_sync_log(log_id, 1, 1, "ok")
    assert main._should_auto_sync() is False

    # Stale successful sync (7h ago) -> True
    old = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
    with test_db.db() as conn:
        conn.execute("UPDATE sync_log SET finished_at = ?", (old,))
    assert main._should_auto_sync() is True


def test_should_auto_sync_disabled_at_zero(test_db, monkeypatch):
    import main
    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 0.0)
    assert main._should_auto_sync() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: FAIL — `main` has no attribute `SYNC_INTERVAL_HOURS`, and the stale case fails against the hardcoded 3600s.

- [ ] **Step 3: Implement in `app/main.py`**

Add imports at the top: `import time` and `import notify`.

Add the constant near the other module globals (after `_gen_last`, line ~24):

```python
SYNC_INTERVAL_HOURS = float(os.environ.get("SYNC_INTERVAL_HOURS", "6"))
```

Replace `_should_auto_sync` and `startup` (lines 41-59):

```python
@app.on_event("startup")
def startup():
    db.init_db()
    syncer.seed_if_empty()
    if SYNC_INTERVAL_HOURS > 0:
        t = threading.Thread(target=_sync_scheduler, daemon=True)
        t.start()


def _should_auto_sync() -> bool:
    """True when the last successful sync is older than SYNC_INTERVAL_HOURS."""
    if SYNC_INTERVAL_HOURS <= 0:
        return False
    last = db.get_last_sync()
    if not last or last["status"] != "ok":
        return True
    try:
        last_time = datetime.fromisoformat(last["finished_at"])
        return (datetime.now() - last_time).total_seconds() > SYNC_INTERVAL_HOURS * 3600
    except (ValueError, TypeError):
        return True


def _sync_scheduler():
    """Daemon loop: check every 15 minutes, sync when the interval has lapsed.

    The first pass also covers the old sync-on-startup behavior.
    """
    while True:
        try:
            if _should_auto_sync():
                _run_sync(1)
        except Exception as exc:
            db.log("sync", f"Scheduled sync error: {exc}", level="error")
        time.sleep(900)
```

In `_run_gen` (line ~27), add notifications after the result is recorded:

```python
def _run_gen(profile_id: int, count: int):
    global _gen_running, _gen_last
    _gen_running = True
    try:
        result = generator.run_generation(profile_id, count)
        _gen_last = {**result, "error": None, "finished_at": datetime.now().isoformat()}
        profile = db.get_profile(profile_id)
        pname = profile["name"] if profile else f"profile {profile_id}"
        notify.notify("✨ Recommendations ready",
                      f"Generated {result['added']} new recommendations for {pname}.")
    except Exception as exc:
        db.log("gen", f"Generation error: {exc}", level="error")
        _gen_last = {"added": 0, "error": str(exc), "finished_at": datetime.now().isoformat()}
        notify.notify("Bookclub generation failed", str(exc), tags="warning", priority="4")
    finally:
        _gen_running = False
        _gen_lock.release()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_scheduler.py
git commit -m "feat: interval-based background sync scheduler + generation notifications"
```

---

### Task 4: Environment plumbing + docs

**Files:**
- Modify: `docker-compose.yml` (environment block)
- Modify: `.env.example`
- Modify: `docs/ops-runbook.md` (env var table), `docs/product-requirements.md` (Sync section, "Future Considerations" — remove the now-done webhook/cron bullet)

**Interfaces:** none (config/docs only).

- [ ] **Step 1: Compose environment passthrough**

In `docker-compose.yml`, add to the `environment:` list:

```yaml
      - SYNC_INTERVAL_HOURS=${SYNC_INTERVAL_HOURS:-6}
      - NTFY_URL=${NTFY_URL}
      - NTFY_TOKEN=${NTFY_TOKEN}
```

- [ ] **Step 2: `.env.example` entries**

Append under App settings:

```
# Hours between automatic background syncs (0 disables; default 6)
SYNC_INTERVAL_HOURS=6

# ── Notifications (optional) ─────────────────────────────────────────────────
# Full ntfy topic URL, e.g. https://ntfy.example.com/bookclub. Empty = disabled.
NTFY_URL=
# Bearer token if your ntfy server requires auth
NTFY_TOKEN=
```

- [ ] **Step 3: Docs**

- `docs/ops-runbook.md` env table: add rows for `SYNC_INTERVAL_HOURS` (No, `6`, "Hours between automatic syncs; 0 disables"), `NTFY_URL` (No, —, "ntfy topic URL for push notifications"), `NTFY_TOKEN` (No, —, "ntfy bearer token").
- `docs/product-requirements.md` → Sync section: add "Background scheduler re-syncs every `SYNC_INTERVAL_HOURS` (default 6h)". Remove the "Hardcover webhook / live sync" bullet from Future Considerations.

- [ ] **Step 4: Apply and verify live**

```bash
cd /mnt/c/Tools/bookclub && docker compose up -d
docker logs --tail 20 bookclub
```

Set a real `NTFY_URL` in `.env` first if available. Trigger a generation from the UI and confirm a ntfy push arrives; check Settings → App Log for scheduler entries after startup.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example docs/ops-runbook.md docs/product-requirements.md
git commit -m "chore: env plumbing and docs for scheduled sync + ntfy"
```
