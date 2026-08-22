# UI Quick Wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three small quality-of-life wins: pending recs sorted by AI confidence, clickable genre tags that filter the grid, and an in-app "Add book" form for word-of-mouth recommendations.

**Architecture:** Confidence becomes the second sort key in the central `get_recommendations()` query. Tag search rides the existing `q` LIKE filter (extended to the `tags` column) with tag chips becoming links to `/?q=<tag>`. The add-book form is a `<details>` disclosure on the Recommendations page posting to a new route that reuses `upsert_recommendation` + the existing Open Library cover fetcher.

**Tech Stack:** SQLite ordering, Jinja2/HTMX, existing `_fetch_missing_covers` background task.

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub`; test harness from Plan 01; run `.venv/bin/pytest -q` from repo root.
- `get_recommendations` is the single profile-aware query (`_REC_COLS`/`_REC_JOINS` pattern) — do not fork a second query path.
- New recs are visible as `pending` to every profile automatically (LEFT JOIN + COALESCE) — the add-book route must NOT insert `rec_interactions` rows.
- SQLite in the container is ≥3.39; `NULLS LAST` is available (already used in `history_page`).
- Match existing CSS class conventions (`btn`, `rec-tag`, `filter-tabs`…) in `app/static/style.css`; add new rules at the end of the file under a `/* ── Add book form ── */` comment.
- Commits: conventional, no AI-attribution lines.

---

### Task 1: Confidence-aware ordering

**Files:**
- Modify: `app/db.py` (`get_recommendations` ORDER BY, lines 273-281)
- Test: `tests/test_ui_quick_wins.py`

**Interfaces:**
- Produces: within each status group, recs order by `in_abs_library DESC`, then `confidence DESC NULLS LAST`, then `title` (previously no confidence key).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ui_quick_wins.py`:

```python
def test_pending_recs_ordered_by_confidence(test_db):
    test_db.upsert_recommendation("Low Conf", "A", None, "Book", "Yes", "r", confidence=40)
    test_db.upsert_recommendation("High Conf", "B", None, "Book", "Yes", "r", confidence=95)
    test_db.upsert_recommendation("No Conf", "C", None, "Book", "Yes", "r")

    recs = test_db.get_recommendations("pending", 1)
    titles = [r["title"] for r in recs]
    assert titles == ["High Conf", "Low Conf", "No Conf"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ui_quick_wins.py -v`
Expected: FAIL — current order is alphabetical (`High Conf, Low Conf, No Conf` happens to be alphabetical too, so make sure the test actually discriminates: rename "No Conf" to "Aardvark Book" in both places so alphabetical order would put it first):

```python
    test_db.upsert_recommendation("Aardvark Book", "C", None, "Book", "Yes", "r")
    ...
    assert titles == ["High Conf", "Low Conf", "Aardvark Book"]
```

- [ ] **Step 3: Implement**

In `app/db.py` `get_recommendations`, change the ORDER BY clause to:

```sql
            ORDER BY
              CASE COALESCE(ri.user_status, 'pending')
                WHEN 'queued'  THEN 1
                WHEN 'pending' THEN 2
                WHEN 'read'    THEN 3
                WHEN 'pass'    THEN 4
              END,
              r.in_abs_library DESC,
              r.confidence DESC NULLS LAST,
              r.title
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ui_quick_wins.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_ui_quick_wins.py
git commit -m "feat: sort recs by AI confidence within each status"
```

---

### Task 2: Clickable tag filtering

**Files:**
- Modify: `app/db.py` (`get_recommendations` q clause, lines 261-266)
- Modify: `app/templates/partials/rec_card.html` (tag chips, lines 55-64)
- Test: `tests/test_ui_quick_wins.py` (extend)

**Interfaces:**
- Produces: `q` also matches the comma-separated `tags` column; tag chips render as `<a href="/?q=<tag>">` keeping classes `rec-tag` / `rec-tag-extra`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_search_matches_tags(test_db):
    test_db.upsert_recommendation("Tagged Book", "A", None, "Book", "Yes", "r",
                                  tags="progression fantasy, litRPG")
    test_db.upsert_recommendation("Other Book", "B", None, "Book", "Yes", "r")

    recs = test_db.get_recommendations("all", 1, q="litrpg")
    assert [r["title"] for r in recs] == ["Tagged Book"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ui_quick_wins.py -v` — FAIL (0 results).

- [ ] **Step 3: Implement**

In `app/db.py` `get_recommendations`, extend the q condition:

```python
        if q:
            conditions.append(
                "(lower(r.title) LIKE :q OR lower(COALESCE(r.author,'')) LIKE :q "
                "OR lower(COALESCE(r.series,'')) LIKE :q "
                "OR lower(COALESCE(r.tags,'')) LIKE :q)"
            )
            params["q"] = f"%{q.lower()}%"
```

In `app/templates/partials/rec_card.html`, replace the tag `<span>` loop (lines 57-59) with links:

```html
      {% for tag in tags %}
        <a class="rec-tag{% if loop.index > 2 %} rec-tag-extra{% endif %}"
           href="/?q={{ tag | urlencode }}" title="Show all “{{ tag }}” recs">{{ tag }}</a>
      {% endfor %}
```

(The `+N more` expand button and the document-level click handler in `base.html` are untouched — they target `.tag-expand-btn` only.)

- [ ] **Step 4: Run tests + visual check**

Run: `.venv/bin/pytest -q` — all pass. In the browser, click a tag chip on any card → grid filters to that tag; the search box shows the tag text.

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/templates/partials/rec_card.html tests/test_ui_quick_wins.py
git commit -m "feat: search matches tags; tag chips are clickable filters"
```

---

### Task 3: Manual "Add book" form

**Files:**
- Modify: `app/main.py` (new route after `api_import_recs`, line ~529)
- Modify: `app/templates/recommendations.html` (below the search form, line ~24)
- Modify: `app/static/style.css` (append small rules)
- Test: `tests/test_ui_quick_wins.py` (extend)

**Interfaces:**
- Consumes: `db.upsert_recommendation(...) -> int`, `main._fetch_missing_covers(recs: list[tuple[int, str, str]])` (existing async background task at line ~531).
- Produces: `POST /recs/add` (form: `title` required; `author`, `series`, `reason` optional) → 303 redirect to `/?q=<title>`; rec has `source="manual"`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_add_book_route_creates_manual_rec(client, test_db):
    resp = client.post("/recs/add", data={
        "title": "Word of Mouth", "author": "A Friend",
        "series": "", "reason": "Recommended at dinner",
    }, follow_redirects=False)
    assert resp.status_code == 303
    with test_db.db() as conn:
        rec = conn.execute(
            "SELECT source, reason FROM recommendations WHERE title = 'Word of Mouth'"
        ).fetchone()
    assert rec["source"] == "manual"
    assert rec["reason"] == "Recommended at dinner"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ui_quick_wins.py -v` — FAIL with 404.

- [ ] **Step 3: Implement the route in `app/main.py`**

```python
@app.post("/recs/add")
def add_rec_manually(request: Request, background_tasks: BackgroundTasks,
                     title: str = Form(...), author: str = Form(""),
                     series: str = Form(""), reason: str = Form("")):
    title = title.strip()
    if not title:
        return RedirectResponse("/", status_code=303)
    rec_id = db.upsert_recommendation(
        title, author.strip() or None, series.strip() or None,
        "Book", "Unknown", reason.strip() or "Added manually.",
        source="manual")
    background_tasks.add_task(_fetch_missing_covers, [(rec_id, title, author.strip())])
    return RedirectResponse(f"/?q={quote(title)}", status_code=303)
```

Add `from urllib.parse import quote` to the imports at the top of `main.py`.

- [ ] **Step 4: Add the form to `app/templates/recommendations.html`**

Directly after the search `</form>` (line 23), insert:

```html
<details class="add-book">
  <summary class="btn">+ Add book</summary>
  <form method="post" action="/recs/add" class="add-book-form">
    <input name="title" required maxlength="200" placeholder="Title *">
    <input name="author" maxlength="120" placeholder="Author">
    <input name="series" maxlength="120" placeholder="Series">
    <input name="reason" maxlength="300" placeholder="Why? (shown as the rec reason)">
    <button class="btn btn-queue" type="submit">Add</button>
  </form>
</details>
```

- [ ] **Step 5: Style it** (append to `app/static/style.css`)

```css
/* ── Add book form ── */
.add-book { margin: 0 0 12px; }
.add-book summary { display: inline-block; cursor: pointer; list-style: none; }
.add-book summary::-webkit-details-marker { display: none; }
.add-book-form { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.add-book-form input {
  flex: 1 1 160px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
}
```

(If `--border` / `--surface` / `--text` are named differently in `style.css`, use the variable names the search input `.rec-search-input` uses — copy its border/background/color declarations.)

- [ ] **Step 6: Run tests, visual check, commit**

Run: `.venv/bin/pytest -q` — all pass. Browser: "+ Add book" expands, submitting lands on the grid filtered to the new title; the Open Library cover appears after a few seconds (refresh).

```bash
git add app/main.py app/templates/recommendations.html app/static/style.css tests/test_ui_quick_wins.py
git commit -m "feat: manual add-book form on the recommendations page"
```

---

### Task 4: Documentation

**Files:**
- Modify: `docs/user-guide.md` (Recommendations Page section), `docs/product-requirements.md` (endpoints table; remove "Bulk rec management"? No — leave; add `POST /recs/add`)

- [ ] **Step 1: Update docs**

- `docs/user-guide.md`: note that pending recs are ordered by confidence, tags are clickable filters, and describe the "+ Add book" form.
- `docs/product-requirements.md`: add `POST /recs/add` to the endpoints table; mention `source="manual"` in the catalog section.

- [ ] **Step 2: Commit**

```bash
git add docs/user-guide.md docs/product-requirements.md
git commit -m "docs: document confidence sort, tag filters, add-book form"
```
