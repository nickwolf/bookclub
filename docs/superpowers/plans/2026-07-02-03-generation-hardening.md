# Generation Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI generation robust: fuzzy (normalized) deduplication so near-duplicate titles can't slip through, and schema-guaranteed JSON via the API's structured outputs so a truncated or chatty response can never lose a whole batch.

**Architecture:** Dedup keys switch from exact lowercase title to `sync._norm()` (the same normalization the sync matcher uses — unicode-normalize, strip articles, collapse whitespace). JSON extraction switches from regex scraping to the Messages API structured-outputs feature (`output_config.format` with a JSON schema), which guarantees a valid JSON object; `max_tokens` rises to 8192. Requires bumping the `anthropic` SDK pin in the Dockerfile (0.51.0 predates `output_config`).

**Tech Stack:** `anthropic` Python SDK ≥ 0.92, structured outputs (`output_config.format` json_schema).

## Global Constraints

- **Depends on Plan 02** (per-user AI settings) being merged first: `run_generation` already resolves `(api_key, model)` via `gen.resolve_ai_settings`, and `gen.MODEL_CHOICES` restricts models to `claude-sonnet-5` / `claude-opus-4-8` / `claude-haiku-4-5` — all of which support structured outputs. Do not add models outside that list here.
- Repo: `/mnt/c/Tools/bookclub`; test harness from Plan 01; run `.venv/bin/pytest -q` from repo root.
- The Dockerfile change in Task 3 requires a container rebuild: `docker compose up -d --build` (works from WSL2 for this project). All other changes hot-reload.
- Structured-outputs JSON schemas must set `additionalProperties: false` on every object and must not use `minItems`/`maxLength`-style constraints (unsupported).
- Commits: conventional, no AI-attribution lines.

---

### Task 1: Extract a pure dedup helper using `_norm`

**Files:**
- Modify: `app/gen.py` (imports line ~10; replace dedup block in `run_generation`, currently lines ~171-191)
- Test: `tests/test_gen_dedup.py`

**Interfaces:**
- Consumes: `sync._norm(title: str) -> str` (exists at `app/sync.py:121`; `sync` does not import `gen`, so no import cycle).
- Produces: `gen._dedup_recs(recs: list[dict], blocked_norms: set[str]) -> list[dict]` — filters out recs whose normalized title is empty, already in `blocked_norms`, or repeated within the batch.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gen_dedup.py`:

```python
from sync import _norm


def test_dedup_drops_article_variants():
    import gen
    blocked = {_norm("The Name of the Wind")}
    recs = [
        {"title": "Name of the Wind", "author": "Rothfuss"},   # variant of blocked
        {"title": "Mistborn", "author": "Sanderson"},
    ]
    out = gen._dedup_recs(recs, blocked)
    assert [r["title"] for r in out] == ["Mistborn"]


def test_dedup_drops_in_batch_duplicates_and_blank_titles():
    import gen
    recs = [
        {"title": "Dune"},
        {"title": "The Dune"},   # normalizes to same key
        {"title": ""},
        {"title": "Hyperion"},
    ]
    out = gen._dedup_recs(recs, set())
    assert [r["title"] for r in out] == ["Dune", "Hyperion"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_gen_dedup.py -v`
Expected: FAIL with `AttributeError: module 'gen' has no attribute '_dedup_recs'`

- [ ] **Step 3: Implement in `app/gen.py`**

Add the import near the top (below `import db`):

```python
from sync import _norm
```

Add the helper above `run_generation`:

```python
def _dedup_recs(recs: list[dict], blocked_norms: set[str]) -> list[dict]:
    """Drop recs whose normalized title is blocked, blank, or repeated in-batch."""
    seen: set[str] = set()
    out = []
    for rec in recs:
        key = _norm(rec.get("title", "") or "")
        if not key or key in blocked_norms or key in seen:
            db.log("gen", f"Skipped duplicate/already-read: {rec.get('title')}", level="info")
            continue
        seen.add(key)
        out.append(rec)
    return out
```

Inside `run_generation`, replace the whole dedup block (from `# Deduplicate: existing recommendations + already-read HC books` through `recs = deduped`) with:

```python
    # Deduplicate against existing recs + already-read HC books,
    # using the same normalization the sync matcher uses.
    with db.db() as conn:
        existing_titles = [row[0] for row in
                           conn.execute("SELECT title FROM recommendations").fetchall()]
    blocked = {_norm(t) for t in existing_titles}
    blocked |= {_norm(t) for t in db.get_hc_read_titles()}
    recs = _dedup_recs(recs, blocked)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_gen_dedup.py -v` — PASS. Then `.venv/bin/pytest -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add app/gen.py tests/test_gen_dedup.py
git commit -m "fix: normalize titles for generation dedup (matches sync fuzzy matching)"
```

---

### Task 2: Structured outputs — schema + parsing

**Files:**
- Modify: `app/gen.py` (add schema constant; change API call + parsing in `run_generation`; simplify prompt tail in `build_prompt` lines ~108-129)
- Test: `tests/test_gen_dedup.py` (extend with a parse test)

**Interfaces:**
- Produces: `gen.REC_SCHEMA: dict` (JSON schema), `gen._parse_recs(text: str) -> list[dict]` (parses the structured-output payload). `extract_json` is deleted (only `gen` used it; `scripts/refresh_recs.py` has its own copy).

- [ ] **Step 1: Write the failing test** (append to `tests/test_gen_dedup.py`)

```python
def test_parse_recs_reads_structured_payload():
    import gen, json
    payload = json.dumps({"recommendations": [
        {"title": "Dune", "author": "Frank Herbert", "series": None,
         "type": "Book", "audiobook_available": "Yes", "confidence": 88,
         "reason": "Fits.", "tags": ["sci-fi"]},
    ]})
    recs = gen._parse_recs(payload)
    assert recs[0]["title"] == "Dune"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_gen_dedup.py -v`
Expected: new test FAILS with `AttributeError`.

- [ ] **Step 3: Implement schema + parser in `app/gen.py`**

Add below `MODEL_CHOICES`:

```python
# Structured-outputs schema. Every object needs additionalProperties: false;
# numeric/string min-max constraints are not supported by the API.
REC_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title":  {"type": "string"},
                    "author": {"type": "string"},
                    "series": {"type": ["string", "null"]},
                    "type":   {"type": "string", "enum": ["Book", "Series"]},
                    "audiobook_available": {"type": "string", "enum": ["Yes", "No", "Partial"]},
                    "confidence": {"type": "integer"},
                    "reason": {"type": "string"},
                    "tags":   {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "author", "series", "type",
                             "audiobook_available", "confidence", "reason", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


def _parse_recs(text: str) -> list[dict]:
    try:
        return json.loads(text)["recommendations"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"Could not parse generation response: {e}") from e
```

Delete the `extract_json` function and the `import re` line (no longer used).

In `run_generation`, replace the API call and extraction:

```python
    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=8192,
            output_config={"format": {"type": "json_schema", "schema": REC_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        db.log("gen", f"Claude API call failed: {e}", level="error")
        raise
    text = next(b.text for b in message.content if b.type == "text")
    recs = _parse_recs(text)
```

> If the installed SDK rejects `output_config` as an unknown kwarg, pass it via `extra_body={"output_config": {...}}` instead — but Task 3 bumps the SDK so the typed param should work.

In `build_prompt`, replace the final format instructions (the last three entries of the `sections += [...]` list, from `"Respond with ONLY a JSON array..."` onward) with a single entry:

```python
        "Return the recommendations in the required JSON structure. The 'reason' "
        "field must be 1-2 sentences on why the book fits their taste — never "
        "meta-commentary about the recommendation process.",
```

(Keep the confidence-calibration and no-duplicates instructions — they still shape content.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (parse test included).

- [ ] **Step 5: Commit**

```bash
git add app/gen.py tests/test_gen_dedup.py
git commit -m "feat: structured-outputs JSON schema for rec generation, drop regex extraction"
```

---

### Task 3: SDK bump + live verification

**Files:**
- Modify: `Dockerfile` (anthropic pin, line 10)

**Interfaces:** none new.

- [ ] **Step 1: Bump the SDK pin**

In `Dockerfile`, change `anthropic==0.51.0` to:

```
    "anthropic>=0.92,<2"
```

Mirror it in the venv:

```bash
.venv/bin/pip install "anthropic>=0.92,<2"
```

- [ ] **Step 2: Rebuild and restart the container**

```bash
cd /mnt/c/Tools/bookclub && docker compose up -d --build
docker logs --tail 10 bookclub
```

Expected: clean start, no import errors. (If compose errors from WSL2, run the same command from a Windows terminal — see repo CLAUDE.md.)

- [ ] **Step 3: Live end-to-end verification**

In the UI: **↺ Recs → Generate 5**. Watch `docker logs -f bookclub` and the in-app log (Settings → App Log):
- `Generation started — requesting 5 recs (model: claude-sonnet-5)`
- `Generation complete — added N recommendations` with N ≤ 5 and no parse errors.
- New recs appear on the Pending tab with confidence badges and tags.

- [ ] **Step 4: Run the suite and commit**

Run: `.venv/bin/pytest -q` — all pass.

```bash
git add Dockerfile
git commit -m "chore: bump anthropic SDK for structured outputs support"
```
