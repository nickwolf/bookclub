# Backup & Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/export` endpoint that streams a consistent snapshot of the SQLite DB as a download, plus a "Download backup" button in Settings — and the pytest harness that every later plan in this series reuses.

**Architecture:** SQLite `VACUUM INTO` produces a consistent single-file snapshot without stopping the app (safe under WAL). A FastAPI route writes the snapshot to a temp file and returns it via `FileResponse` with a background cleanup task. This plan also bootstraps `tests/` + a host venv, since the repo has no test infrastructure yet.

**Tech Stack:** FastAPI 0.115, SQLite (stdlib `sqlite3`), pytest (host venv).

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub` (git, branch `main`). All paths below are relative to repo root.
- Dependencies are pinned in `Dockerfile` (there is no requirements.txt). This plan adds **zero** runtime dependencies.
- Timestamps written by app code use `db._now()`, never SQLite `CURRENT_TIMESTAMP`.
- `recommendations` is a global catalog — per-profile state lives only in `rec_interactions` and `queue`.
- Deployment: `./app` is bind-mounted into the container and uvicorn runs with `--reload`, so `app/` code changes apply live without rebuild. Rebuild (`docker compose up -d --build`) only if the Dockerfile changes.
- Read `docs/ops-runbook.md` → "Known Gotchas" before touching sync, HTMX, or ABS code.
- Commits: conventional (`feat:`, `fix:`, `test:`, `chore:`), one purpose each. **Never add `Co-Authored-By` or any AI-attribution lines to commits.**
- Run tests from repo root: `.venv/bin/pytest -q`

---

### Task 1: Test harness bootstrap

**Files:**
- Create: `conftest.py` (repo root)
- Create: `tests/test_harness.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: pytest fixtures `test_db` (yields the `db` module bound to a throwaway SQLite file) and `client` (a `fastapi.testclient.TestClient` for `main.app` that does **not** run startup events — so no seed data, no auto-sync network calls). Every later plan's tests consume these two fixtures.

- [ ] **Step 1: Create the venv and install pinned deps + pytest**

```bash
cd /mnt/c/Tools/bookclub
python3 -m venv .venv
.venv/bin/pip install fastapi==0.115.0 uvicorn==0.30.6 jinja2==3.1.4 \
    python-multipart==0.0.9 httpx==0.27.2 anthropic==0.51.0 pytest==8.3.4
```

Expected: installs complete without error. (Versions match the Dockerfile pins; if a later plan bumps the Dockerfile, bump the venv the same way.)

- [ ] **Step 2: Append test artifacts to `.gitignore`**

Append these lines to `.gitignore` (current content ends with `*.db-wal`):

```
.venv/
.pytest_cache/
```

- [ ] **Step 3: Write `conftest.py`**

```python
"""Shared pytest fixtures.

The app modules live in app/ and use paths relative to that directory
(StaticFiles(directory="static"), Jinja2Templates(directory="templates")),
so we chdir into app/ before anything imports main.
"""
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent / "app"
sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)

import pytest


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    """The db module pointed at a throwaway SQLite file, schema initialised."""
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db


@pytest.fixture()
def client(test_db):
    """TestClient for the app.

    Deliberately NOT used as a context manager: the startup hook seeds
    ~30 recommendations and can spawn an auto-sync thread that calls the
    Hardcover API. A plain TestClient never runs startup events.
    """
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)
```

- [ ] **Step 4: Write a sanity test proving the fixtures work**

Create `tests/test_harness.py`:

```python
def test_db_fixture_creates_schema(test_db):
    with test_db.db() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"profiles", "recommendations", "rec_interactions", "queue"} <= tables


def test_default_profile_seeded(test_db):
    p = test_db.get_profile(1)
    assert p is not None


def test_client_serves_home(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Recommendations" in resp.text
```

- [ ] **Step 5: Run the tests**

Run: `cd /mnt/c/Tools/bookclub && .venv/bin/pytest -q`
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add conftest.py tests/test_harness.py .gitignore
git commit -m "test: add pytest harness with isolated-DB and TestClient fixtures"
```

---

### Task 2: `db.backup_db()` snapshot function

**Files:**
- Modify: `app/db.py` (add function after `get_conn`, around line 20)
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces: `db.backup_db(dest_path: str) -> None` — writes a consistent snapshot of the live DB to `dest_path`. Raises `sqlite3.OperationalError` if `dest_path` already exists (VACUUM INTO requirement).

- [ ] **Step 1: Write the failing test**

Create `tests/test_backup.py`:

```python
import sqlite3


def test_backup_db_produces_consistent_snapshot(test_db, tmp_path):
    # put a recognisable row in the live DB
    rec_id = test_db.upsert_recommendation(
        "Backup Test Book", "Author A", None, "Book", "Yes", "reason")
    dest = tmp_path / "snapshot.db"

    test_db.backup_db(str(dest))

    snap = sqlite3.connect(dest)
    try:
        title = snap.execute(
            "SELECT title FROM recommendations WHERE id = ?", (rec_id,)
        ).fetchone()[0]
    finally:
        snap.close()
    assert title == "Backup Test Book"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backup.py -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'backup_db'`

- [ ] **Step 3: Implement `backup_db` in `app/db.py`**

Insert directly below the `db()` context manager (after line 32):

```python
def backup_db(dest_path: str):
    """Write a consistent snapshot of the live DB to dest_path.

    Uses VACUUM INTO, which is safe while the app is running (WAL mode).
    dest_path must not already exist.
    """
    conn = get_conn()
    try:
        conn.execute("VACUUM INTO ?", (dest_path,))
    finally:
        conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_backup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_backup.py
git commit -m "feat: add db.backup_db consistent snapshot via VACUUM INTO"
```

---

### Task 3: `GET /export` download route

**Files:**
- Modify: `app/main.py` (new route in the "Settings / log" section, after `clear_log`, ~line 660)
- Test: `tests/test_backup.py` (extend)

**Interfaces:**
- Consumes: `db.backup_db(dest_path)` from Task 2.
- Produces: `GET /export` → `200` with `application/octet-stream` body (a valid SQLite file), `Content-Disposition` filename `bookclub-YYYYMMDD-HHMMSS.db`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_backup.py`)

```python
def test_export_route_returns_sqlite_file(client):
    resp = client.get("/export")
    assert resp.status_code == 200
    assert resp.content[:16] == b"SQLite format 3\x00"
    assert "bookclub-" in resp.headers["content-disposition"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backup.py -v`
Expected: the new test FAILS with 404.

- [ ] **Step 3: Implement the route in `app/main.py`**

Add imports at the top of `main.py` (it already imports `os`, `threading`, `datetime`):

```python
import tempfile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
```

(`FileResponse` can be added to the existing `fastapi.responses` import line.)

Add the route in the Settings section:

```python
@app.get("/export")
def export_db():
    """Download a consistent snapshot of the database."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.unlink(tmp.name)  # VACUUM INTO requires the target not to exist
    db.backup_db(tmp.name)
    return FileResponse(
        tmp.name,
        filename=f"bookclub-{ts}.db",
        media_type="application/octet-stream",
        background=BackgroundTask(os.unlink, tmp.name),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_backup.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_backup.py
git commit -m "feat: add GET /export database backup download"
```

---

### Task 4: Settings button + runbook documentation

**Files:**
- Modify: `app/templates/settings.html` (Sync History tab, line ~119)
- Modify: `docs/ops-runbook.md` (Backup section, ~line 78)

**Interfaces:**
- Consumes: `GET /export` from Task 3.

- [ ] **Step 1: Add the download button to the Sync History tab**

In `app/templates/settings.html`, immediately after `<div id="tab-sync" class="settings-tab-panel" hidden>` (line 119), insert:

```html
  <div class="settings-card-header" style="margin-bottom:12px">
    <span></span>
    <a class="btn" href="/export" download>⬇ Download backup</a>
  </div>
```

- [ ] **Step 2: Verify in the browser**

Run: `docker ps | grep bookclub` (container running; `--reload` picks up the change).
Open `http://localhost:8585/settings#sync` — the button appears and clicking it downloads `bookclub-<timestamp>.db`.

- [ ] **Step 3: Update the runbook**

In `docs/ops-runbook.md`, replace the `### Backup` section body (currently just the `docker cp` command) with:

```markdown
### Backup

**From the UI:** Settings → Sync History → "⬇ Download backup", or fetch
`http://localhost:8585/export` from any LAN machine. This is a consistent
snapshot (`VACUUM INTO`) — safe while the app is running.

```bash
# Scriptable equivalent (e.g. from a scheduled task feeding offsite backup)
curl -sf -o "bookclub-$(date +%Y%m%d).db" http://localhost:8585/export
```

**Fallback (container-level copy):**

```bash
docker cp bookclub:/data/bookclub.db ./bookclub-backup-$(date +%Y%m%d).db
```
```

- [ ] **Step 4: Run full test suite and commit**

Run: `.venv/bin/pytest -q`
Expected: all pass.

```bash
git add app/templates/settings.html docs/ops-runbook.md
git commit -m "feat: backup download button in Settings + runbook backup docs"
```
