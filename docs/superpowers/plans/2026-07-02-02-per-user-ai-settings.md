# Per-User API Keys & Model Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each profile can store its own Anthropic API key and pick which Claude model generates its recommendations, with the shared `.env` key/model as fallback — and the app default moves from `claude-sonnet-4-6` to `claude-sonnet-5`.

**Architecture:** Two new nullable columns on `profiles` (`anthropic_api_key`, `anthropic_model`), following the exact pattern of the existing per-profile `abs_token`. A `gen.resolve_ai_settings(profile_id)` helper centralises "profile override → env fallback". The Settings → Users configure panel gets a third section (key input + model dropdown), mirroring the existing ABS-token section. Model choices are a fixed allowlist of current models that all support structured outputs (so Plan 03 can rely on it).

**Tech Stack:** FastAPI 0.115, SQLite, Jinja2/HTMX, `anthropic` SDK (no version change needed in this plan).

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub`. Test harness (`conftest.py` fixtures `test_db`, `client`) exists from Plan 01 — reuse it; run `.venv/bin/pytest -q` from repo root.
- Column migrations use the established pattern in `db.init_db()`: an `ALTER TABLE` string in the migration list at `app/db.py:119-134`, wrapped in try/except `sqlite3.OperationalError`.
- Timestamps via `db._now()`, never `CURRENT_TIMESTAMP` in app writes.
- API keys are stored plaintext in SQLite. This is accepted for a LAN-only household app (same as the existing `abs_token`); render them only in `type="password"` inputs.
- **Allowed model IDs (exact strings, do not invent variants):** `claude-sonnet-5`, `claude-opus-4-8`, `claude-haiku-4-5`. New default: `claude-sonnet-5`.
- Deployment: `app/` code changes hot-reload. Changing `docker-compose.yml`/`.env` requires `docker compose up -d` (works from WSL2 for this project — no Windows-style paths in this compose file).
- Commits: conventional, no AI-attribution lines.

---

### Task 1: Profile columns + DB helper

**Files:**
- Modify: `app/db.py` (migration list line ~134; new helper after `update_profile_picks_playlist_id`, line ~210)
- Test: `tests/test_ai_settings.py`

**Interfaces:**
- Produces: `db.update_profile_ai_settings(profile_id: int, api_key: str, model: str) -> None`. Empty strings are stored as `NULL`. Profile rows gain `anthropic_api_key` and `anthropic_model` keys.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ai_settings.py`:

```python
def test_update_profile_ai_settings_roundtrip(test_db):
    test_db.update_profile_ai_settings(1, "sk-ant-test123", "claude-opus-4-8")
    p = test_db.get_profile(1)
    assert p["anthropic_api_key"] == "sk-ant-test123"
    assert p["anthropic_model"] == "claude-opus-4-8"


def test_update_profile_ai_settings_blank_clears(test_db):
    test_db.update_profile_ai_settings(1, "sk-ant-test123", "claude-opus-4-8")
    test_db.update_profile_ai_settings(1, "", "")
    p = test_db.get_profile(1)
    assert p["anthropic_api_key"] is None
    assert p["anthropic_model"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ai_settings.py -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'update_profile_ai_settings'`

- [ ] **Step 3: Implement migration + helper**

In `app/db.py`, append two entries to the migration list (after `"ALTER TABLE profiles ADD COLUMN abs_picks_playlist_id TEXT",` line 133):

```python
            "ALTER TABLE profiles ADD COLUMN anthropic_api_key TEXT",
            "ALTER TABLE profiles ADD COLUMN anthropic_model TEXT",
```

Add the helper after `update_profile_picks_playlist_id` (line ~210):

```python
def update_profile_ai_settings(profile_id: int, api_key: str, model: str):
    with db() as conn:
        conn.execute(
            "UPDATE profiles SET anthropic_api_key = ?, anthropic_model = ? WHERE id = ?",
            (api_key.strip() or None, model.strip() or None, profile_id)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ai_settings.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_ai_settings.py
git commit -m "feat: per-profile anthropic_api_key and anthropic_model columns"
```

---

### Task 2: Resolution logic in gen.py + new default model

**Files:**
- Modify: `app/gen.py` (top constants lines 14-19, `run_generation` lines 144-168)
- Test: `tests/test_ai_settings.py` (extend)

**Interfaces:**
- Consumes: `db.get_profile(profile_id)` with the new columns from Task 1.
- Produces:
  - `gen.MODEL_CHOICES: list[tuple[str, str]]` — `(model_id, human_label)` pairs.
  - `gen.resolve_ai_settings(profile_id: int) -> tuple[str, str]` — returns `(api_key, model)`; profile value wins, else env (`ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`, new default `claude-sonnet-5`).
  - `gen.api_key_configured(profile_id: int | None = None) -> bool` — with an ID, checks the resolved key; with `None`, checks only the env key (back-compat).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ai_settings.py`)

```python
def test_resolve_prefers_profile_values(test_db, monkeypatch):
    import gen
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "sk-env-key")
    monkeypatch.setattr(gen, "ANTHROPIC_MODEL", "claude-sonnet-5")
    test_db.update_profile_ai_settings(1, "sk-profile-key", "claude-opus-4-8")

    key, model = gen.resolve_ai_settings(1)
    assert key == "sk-profile-key"
    assert model == "claude-opus-4-8"


def test_resolve_falls_back_to_env(test_db, monkeypatch):
    import gen
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "sk-env-key")
    monkeypatch.setattr(gen, "ANTHROPIC_MODEL", "claude-sonnet-5")

    key, model = gen.resolve_ai_settings(1)   # profile 1 has no overrides
    assert key == "sk-env-key"
    assert model == "claude-sonnet-5"


def test_api_key_configured_per_profile(test_db, monkeypatch):
    import gen
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "")
    assert gen.api_key_configured(1) is False
    test_db.update_profile_ai_settings(1, "sk-profile-key", "")
    assert gen.api_key_configured(1) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ai_settings.py -v`
Expected: 3 new FAIL with `AttributeError: module 'gen' has no attribute 'resolve_ai_settings'`

- [ ] **Step 3: Implement in `app/gen.py`**

Replace lines 14-19 (the two constants and `api_key_configured`) with:

```python
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# Fixed allowlist. All of these support structured outputs (required by
# the generation-hardening plan). Exact IDs — do not append date suffixes.
MODEL_CHOICES = [
    ("claude-sonnet-5",  "Claude Sonnet 5 — best quality/cost balance"),
    ("claude-opus-4-8",  "Claude Opus 4.8 — highest quality"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 — fastest and cheapest"),
]


def resolve_ai_settings(profile_id: int) -> tuple[str, str]:
    """(api_key, model) for a profile: profile override wins, else env."""
    p = db.get_profile(profile_id)
    key   = (p["anthropic_api_key"] or "") if p else ""
    model = (p["anthropic_model"] or "") if p else ""
    return (key or ANTHROPIC_API_KEY, model or ANTHROPIC_MODEL)


def api_key_configured(profile_id: int | None = None) -> bool:
    if profile_id is None:
        return bool(ANTHROPIC_API_KEY)
    return bool(resolve_ai_settings(profile_id)[0])
```

In `run_generation` (line ~144), replace the key check and client/model usage:

```python
def run_generation(profile_id: int, count: int) -> dict:
    """
    Synchronous — intended to run in a background thread.
    Returns {"added": N} on success, raises on failure.
    """
    api_key, model = resolve_ai_settings(profile_id)
    if not api_key:
        raise RuntimeError(
            "No Anthropic API key configured. Add one for this user in "
            "Settings → Users, or set ANTHROPIC_API_KEY in .env and restart."
        )

    db.log("gen", f"Generation started — requesting {count} recs (model: {model})")

    ctx = db.get_rec_context(profile_id)
    prompt = build_prompt(ctx, count)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
```

(The rest of `run_generation` is unchanged in this plan.)

- [ ] **Step 4: Run all tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gen.py tests/test_ai_settings.py
git commit -m "feat: per-profile AI key/model resolution, default model claude-sonnet-5"
```

---

### Task 3: Routes — save AI settings, profile-aware generation gate

**Files:**
- Modify: `app/main.py` (new route after `save_abs_token` line ~326; update `recs_refresh_page` line ~449 and `recs_generate` line ~459; update `settings_page` line ~647)
- Test: `tests/test_ai_settings.py` (extend)

**Interfaces:**
- Consumes: `db.update_profile_ai_settings`, `gen.MODEL_CHOICES`, `gen.resolve_ai_settings`, `gen.api_key_configured(profile_id)`.
- Produces: `POST /profiles/{id}/ai-settings` (form fields `anthropic_api_key`, `anthropic_model`). Template contexts: `settings_page` gains `model_choices` + `default_model`; `recs_refresh_page` gains `model_in_use`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ai_settings.py`)

```python
def test_ai_settings_route_saves(client, test_db):
    resp = client.post("/profiles/1/ai-settings",
                       data={"anthropic_api_key": "sk-ant-abc",
                             "anthropic_model": "claude-opus-4-8"},
                       follow_redirects=False)
    assert resp.status_code == 303
    p = test_db.get_profile(1)
    assert p["anthropic_api_key"] == "sk-ant-abc"
    assert p["anthropic_model"] == "claude-opus-4-8"


def test_ai_settings_route_rejects_unknown_model(client, test_db):
    client.post("/profiles/1/ai-settings",
                data={"anthropic_api_key": "", "anthropic_model": "gpt-9000"},
                follow_redirects=False)
    p = test_db.get_profile(1)
    assert p["anthropic_model"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ai_settings.py -v`
Expected: new tests FAIL (404 on the route).

- [ ] **Step 3: Implement in `app/main.py`**

New route after `save_abs_token` (line ~326):

```python
@app.post("/profiles/{profile_id}/ai-settings")
def save_ai_settings(profile_id: int, request: Request,
                     anthropic_api_key: str = Form(""),
                     anthropic_model: str = Form("")):
    if anthropic_model and anthropic_model not in dict(generator.MODEL_CHOICES):
        anthropic_model = ""
    db.update_profile_ai_settings(profile_id, anthropic_api_key, anthropic_model)
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="save-ok">Saved ✓</span>')
    return RedirectResponse("/settings", status_code=303)
```

Update `recs_refresh_page` (line ~449) so the gate and model line are profile-aware:

```python
@app.get("/recs/refresh", response_class=HTMLResponse)
def recs_refresh_page(request: Request):
    profile_id = get_profile_id(request)
    _, model_in_use = generator.resolve_ai_settings(profile_id)
    return _tmpl(request, "recs_refresh.html",
                 profile_id=profile_id,
                 api_key_configured=generator.api_key_configured(profile_id),
                 model_in_use=model_in_use,
                 gen_running=_gen_running,
                 gen_last=_gen_last)
```

Update the gate inside `recs_generate` (line ~463): change `if not generator.api_key_configured():` to `if not generator.api_key_configured(profile_id):` and its error message to `"No Anthropic API key configured for this user"`.

Update `settings_page` (line ~647) to pass the dropdown data:

```python
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    app_log = db.get_app_log(200)
    sync_history = db.get_sync_history(20)
    return _tmpl(request, "settings.html", app_log=app_log,
                 sync_history=sync_history,
                 model_choices=generator.MODEL_CHOICES,
                 default_model=generator.ANTHROPIC_MODEL)
```

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_ai_settings.py
git commit -m "feat: ai-settings route and profile-aware generation gate"
```

---

### Task 4: Settings UI + generation page model display

**Files:**
- Modify: `app/templates/settings.html` (user edit panel, after the ABS-token section closing `</div>` at line 100)
- Modify: `app/templates/recs_refresh.html` (lines 10-48)

**Interfaces:**
- Consumes: template context vars `model_choices`, `default_model` (settings) and `model_in_use` (recs_refresh); route `POST /profiles/{id}/ai-settings`.

- [ ] **Step 1: Add the AI settings section to the user configure panel**

In `app/templates/settings.html`, after the ABS token `user-edit-section` closes (line 100), insert:

```html
            <div class="user-edit-section">
              <div class="pref-label">
                Anthropic API key
                <span class="pref-hint">Optional — overrides the shared .env key for this user. console.anthropic.com</span>
              </div>
              <form hx-post="/profiles/{{ p.id }}/ai-settings"
                    hx-target="#ai-status-{{ p.id }}"
                    hx-swap="innerHTML"
                    class="pref-form">
                <div class="abs-token-row">
                  <input type="password"
                         name="anthropic_api_key"
                         class="pref-input abs-token-input"
                         placeholder="sk-ant-…"
                         value="{{ p.anthropic_api_key or '' }}"
                         autocomplete="off">
                </div>
                <div class="pref-label" style="margin-top:10px">Claude model</div>
                <div class="abs-token-row">
                  <select name="anthropic_model" class="gen-select">
                    <option value="" {% if not p.anthropic_model %}selected{% endif %}>
                      App default ({{ default_model }})
                    </option>
                    {% for mid, label in model_choices %}
                    <option value="{{ mid }}" {% if p.anthropic_model == mid %}selected{% endif %}>{{ label }}</option>
                    {% endfor %}
                  </select>
                  <button type="submit" class="btn rec-notes-save">Save</button>
                </div>
                <span id="ai-status-{{ p.id }}" class="note-status"></span>
              </form>
            </div>
```

- [ ] **Step 2: Show the resolved model on the generation page**

In `app/templates/recs_refresh.html`:

Inside the `{% if api_key_configured %}` card, after the `<p>Claude will read…</p>` line (line 14), add:

```html
  <p class="refresh-hint">Model: <strong>{{ model_in_use }}</strong> — change it per user in <a href="/settings#users">Settings → Users</a>.</p>
```

Replace the `{% else %}` "API key not configured" card body (lines 39-46) with:

```html
<div class="refresh-card">
  <h2>API key not configured</h2>
  <p>Add an Anthropic API key for this user in <a href="/settings#users">Settings → Users → Configure</a>,
     or set a shared key in <code>.env</code> and restart the container:</p>
  <div class="code-block">
    <code>ANTHROPIC_API_KEY=sk-ant-...</code>
  </div>
  <p style="margin-top: 16px">You can get an API key at <strong>console.anthropic.com</strong>.</p>
</div>
```

- [ ] **Step 3: Verify in the browser**

Open `http://localhost:8585/settings#users` → Configure a user → the new section renders; saving shows "Saved ✓". Open `http://localhost:8585/recs/refresh` → model line shows the chosen model.

- [ ] **Step 4: Run the suite and commit**

Run: `.venv/bin/pytest -q` — all pass.

```bash
git add app/templates/settings.html app/templates/recs_refresh.html
git commit -m "feat: per-user API key + model picker UI"
```

---

### Task 5: Default-model sweep across config and docs

**Files:**
- Modify: `docker-compose.yml` (line with `ANTHROPIC_MODEL`)
- Modify: `.env.example` (ANTHROPIC_MODEL lines)
- Modify: `.env` (only if it pins the old model)
- Modify: `docs/ops-runbook.md` (env var table, line ~190), `docs/product-requirements.md` (tech-stack table line ~137 and any `claude-sonnet-4-6` mentions), `README.md` (if it mentions the model)

**Interfaces:** none (config/docs only).

- [ ] **Step 1: Sweep the old default**

Run: `grep -rn "claude-sonnet-4-6" --include="*.yml" --include="*.example" --include="*.md" --include="*.py" .`

Replace every occurrence with `claude-sonnet-5`:
- `docker-compose.yml`: `- ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-claude-sonnet-5}`
- `.env.example`: comment + `ANTHROPIC_MODEL=claude-sonnet-5`
- `docs/ops-runbook.md` env table default column
- `docs/product-requirements.md` AI row: `Anthropic API (default claude-sonnet-5, per-user model override)`
- If the live `.env` sets `ANTHROPIC_MODEL=claude-sonnet-4-6`, update it to `claude-sonnet-5`.

- [ ] **Step 2: Recreate the container so the new compose default applies**

```bash
cd /mnt/c/Tools/bookclub && docker compose up -d
docker logs --tail 5 bookclub
```

Expected: container recreated cleanly. Trigger one generation from the UI and confirm the app log line says `model: claude-sonnet-5` (or the profile's override).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml .env.example docs/ops-runbook.md docs/product-requirements.md README.md
git commit -m "chore: default generation model claude-sonnet-4-6 -> claude-sonnet-5"
```
