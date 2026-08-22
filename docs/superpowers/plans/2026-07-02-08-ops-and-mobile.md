# Ops & Mobile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Health/stats endpoints with a compose healthcheck (and a homepage-dashboard widget recipe), a mobile hamburger nav, an installable PWA manifest, and an env-gated `--reload`.

**Architecture:** `/health` does a trivial DB probe; `/api/stats` exposes `get_stats()` as JSON for the homepage `customapi` widget. The compose healthcheck uses `urllib` (slim image has no curl). The nav collapses behind a hamburger under 720px via CSS + a small toggle. PWA uses an SVG icon (Chromium/Android support `image/svg+xml` manifest icons — accepted limitation for iOS, documented).

**Tech Stack:** FastAPI, Docker Compose healthcheck, CSS media queries, Web App Manifest.

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub`; test harness from Plan 01; run `.venv/bin/pytest -q` from repo root.
- Compose changes require `docker compose up -d` (container recreate — fine from WSL2 for this project; if volume errors appear, run from a Windows terminal per repo CLAUDE.md).
- Keep the design system: colors/typography per `docs/design-system.md`; theme variable names come from `app/static/style.css` — reuse, don't invent.
- Commits: conventional, no AI-attribution lines.

---

### Task 1: `/health` + `/api/stats` + compose healthcheck

**Files:**
- Modify: `app/main.py` (routes near the sync section, line ~555)
- Modify: `docker-compose.yml` (healthcheck block)
- Modify: `docs/ops-runbook.md` (troubleshooting section note + homepage widget recipe)
- Test: `tests/test_ops.py`

**Interfaces:**
- Produces: `GET /health` → `{"status": "ok"}` 200 (503 with `{"status": "error"}` if the DB probe fails); `GET /api/stats?profile_id=N` → JSON of `db.get_stats(profile_id)` plus `last_sync` ISO string or null.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ops.py`:

```python
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_api_stats_shape(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "pending" in body and "queued" in body and "last_sync" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ops.py -v` — FAIL with 404.

- [ ] **Step 3: Implement routes in `app/main.py`**

```python
@app.get("/health")
def health():
    try:
        with db.db() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "error"}, status_code=503)


@app.get("/api/stats")
def api_stats(profile_id: int = 1):
    stats = db.get_stats(profile_id)
    last = db.get_last_sync()
    stats["last_sync"] = last["finished_at"] if last and last["finished_at"] else None
    return JSONResponse(stats)
```

- [ ] **Step 4: Compose healthcheck**

In `docker-compose.yml`, add under the `bookclub:` service (sibling of `restart:`):

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=5)"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
```

- [ ] **Step 5: Document the homepage widget**

Append to `docs/ops-runbook.md`:

```markdown
## Homepage Dashboard Widget

The homepage instance (C:\Tools\homepage) can show live stats via the customapi widget:

```yaml
- Bookclub:
    icon: mdi-book-open-variant
    href: http://<host>:8585
    widget:
      type: customapi
      url: http://<host>:8585/api/stats
      mappings:
        - field: pending
          label: Pending
        - field: queued
          label: Queued
        - field: in_library_pending
          label: In library
```
```

- [ ] **Step 6: Run tests, apply, verify**

Run: `.venv/bin/pytest -q` — all pass. Then `docker compose up -d`, wait ~90s, and `docker ps` shows `(healthy)` for bookclub.

- [ ] **Step 7: Commit**

```bash
git add app/main.py docker-compose.yml docs/ops-runbook.md tests/test_ops.py
git commit -m "feat: health and stats endpoints with compose healthcheck"
```

---

### Task 2: PWA manifest + icon

**Files:**
- Create: `app/static/manifest.json`
- Create: `app/static/icon.svg`
- Modify: `app/templates/base.html` (head, after the favicon line 7)
- Test: `tests/test_ops.py` (extend)

**Interfaces:**
- Produces: `/static/manifest.json` and `/static/icon.svg` served; `<link rel="manifest">` + `theme-color` meta in every page head.

- [ ] **Step 1: Write the failing test** (append to `tests/test_ops.py`)

```python
def test_manifest_served(client):
    resp = client.get("/static/manifest.json")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Bookclub"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ops.py -v` — FAIL with 404.

- [ ] **Step 3: Create the files**

`app/static/icon.svg`:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="20" fill="#faf7f2"/>
  <text x="50" y="50" font-size="60" text-anchor="middle" dominant-baseline="central">📚</text>
</svg>
```

(Swap `#faf7f2` for the light-theme background variable value in `app/static/style.css` if it differs — check `docs/design-system.md`.)

`app/static/manifest.json`:

```json
{
  "name": "Bookclub",
  "short_name": "Bookclub",
  "description": "Personal reading companion — recommendations, queue, and history",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#faf7f2",
  "theme_color": "#faf7f2",
  "icons": [
    { "src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any" }
  ]
}
```

In `app/templates/base.html`, after the favicon `<link>` (line 7), add:

```html
  <link rel="manifest" href="/static/manifest.json">
  <meta name="theme-color" content="#faf7f2">
```

> Known limitation (document, don't fix): SVG manifest icons install fine on Chromium/Android; iOS home-screen install would need PNG `apple-touch-icon`s — out of scope.

- [ ] **Step 4: Run tests + verify**

Run: `.venv/bin/pytest tests/test_ops.py -v` — PASS. On an Android phone / desktop Chrome, the install prompt is available (address-bar install icon).

- [ ] **Step 5: Commit**

```bash
git add app/static/manifest.json app/static/icon.svg app/templates/base.html
git commit -m "feat: PWA manifest and app icon"
```

---

### Task 3: Hamburger nav for mobile

**Files:**
- Modify: `app/templates/base.html` (nav block lines 22-70 + script block)
- Modify: `app/static/style.css` (append media query rules)

**Interfaces:**
- Produces: below 720px, `.nav-links` hides behind a `#nav-toggle` button; the existing outside-click handler also closes it.

- [ ] **Step 1: Add the toggle button**

In `app/templates/base.html`, right after `<a href="/" class="nav-brand">📚 Bookclub</a>` (line 23), add:

```html
    <button class="nav-hamburger" id="nav-toggle" aria-label="Menu"
            onclick="document.querySelector('.nav-links').classList.toggle('open')">☰</button>
```

In the `document.addEventListener('click', ...)` outside-click handler (line ~105), extend it to also close the mobile menu:

```javascript
      document.addEventListener('click', e => {
        const menu = document.getElementById('profile-menu');
        if (menu && !menu.contains(e.target)) menu.classList.remove('open');
        const links = document.querySelector('.nav-links');
        const burger = document.getElementById('nav-toggle');
        if (links && burger && !links.contains(e.target) && !burger.contains(e.target)) {
          links.classList.remove('open');
        }
      });
```

- [ ] **Step 2: Add the CSS** (append to `app/static/style.css`)

```css
/* ── Mobile nav ── */
.nav-hamburger { display: none; background: none; border: none; font-size: 22px;
                 cursor: pointer; color: inherit; padding: 4px 8px; }

@media (max-width: 720px) {
  .nav-hamburger { display: inline-block; }
  .nav-links {
    display: none;
    position: absolute;
    top: 100%; left: 0; right: 0;
    flex-direction: column;
    background: inherit;
    border-bottom: 1px solid rgba(0,0,0,0.08);
    padding: 8px 16px 12px;
    z-index: 50;
  }
  .nav-links.open { display: flex; }
  nav { position: relative; flex-wrap: wrap; }
}
```

> Inspect how `nav` and `.nav-links` are currently laid out in `style.css` (81 rules) and adapt: the dropdown background must use the nav's actual background variable (not `inherit` if nav is transparent), and any existing responsive rules for nav must be reconciled rather than duplicated.

- [ ] **Step 3: Verify at mobile width**

Use the playwright-cli or a browser at 400px width: hamburger appears, opens/closes the link list, links navigate, outside-click closes. At desktop width nothing changes. Verify in **both** light and dark themes (◑ toggle).

- [ ] **Step 4: Commit**

```bash
git add app/templates/base.html app/static/style.css
git commit -m "feat: hamburger navigation under 720px"
```

---

### Task 4: Env-gated `--reload`

**Files:**
- Modify: `docker-compose.yml` (command line)
- Modify: `.env.example`
- Modify: `docs/ops-runbook.md` (env table)

**Interfaces:**
- Produces: `UVICORN_RELOAD=1` (default) keeps live-reload; empty/unset value runs without the file watcher.

- [ ] **Step 1: Change the compose command**

Replace the `command:` line in `docker-compose.yml`:

```yaml
    command: sh -c "uvicorn main:app --host 0.0.0.0 --port 8080 $${UVICORN_RELOAD:+--reload}"
```

And add to the `environment:` list:

```yaml
      - UVICORN_RELOAD=${UVICORN_RELOAD:-1}
```

(`$$` escapes the dollar so the shell inside the container — not compose — expands it.)

- [ ] **Step 2: `.env.example` + runbook**

```
# Set empty to disable uvicorn live-reload (slightly lower idle CPU; requires
# restart to pick up code changes): UVICORN_RELOAD=
UVICORN_RELOAD=1
```

Add the row to the ops-runbook env table.

- [ ] **Step 3: Apply and verify both modes**

```bash
docker compose up -d && docker logs --tail 5 bookclub
```

Expected: log shows `Uvicorn running` with reloader active. Then set `UVICORN_RELOAD=` in `.env`, `docker compose up -d`, confirm the reloader line is gone and the app still serves. Restore `UVICORN_RELOAD=1`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example docs/ops-runbook.md
git commit -m "chore: make uvicorn --reload opt-out via UVICORN_RELOAD"
```
