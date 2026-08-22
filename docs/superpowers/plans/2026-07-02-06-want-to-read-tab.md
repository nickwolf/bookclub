# Want-to-Read Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Want to Read" tab on the Recommendations page showing the Hardcover want-to-read shelf, with books already owned in Audiobookshelf surfaced first (badge + sort), and a one-click "+ Queue".

**Architecture:** `hc_books` gains an `in_abs_library` flag updated during `sync_abs()` (same fuzzy matcher used for recs). A new `want_to_read` pseudo-status on the existing recommendations route renders a dedicated grid partial instead of the rec grid — search (`q`) keeps working. Queuing a WTR book creates a rec linked back to the Hardcover row (so it inherits the HC cover) and reuses `add_to_queue`.

**Tech Stack:** SQLite migration, existing fuzzy matcher, Jinja2 partial reusing `.rec-card` styles.

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub`; test harness from Plan 01; run `.venv/bin/pytest -q` from repo root.
- **Ordering note:** Plan 04 changes `sync_abs` to return a tuple. If Plan 04 landed, add this plan's WTR block just before that function's `return`; if not, before the `return updated` line. Either sequencing works.
- `hc_books` is the single household Hardcover shelf (one `HARDCOVER_TOKEN`), so this tab is shared across profiles by design — the queue action is per-profile.
- Migration pattern: `ALTER TABLE` string in the `init_db` migration list with try/except (`app/db.py:119-138`).
- HTMX search on the recommendations page swaps `#rec-results` — the WTR grid must live inside that same target so live search keeps working.
- Commits: conventional, no AI-attribution lines.

---

### Task 1: `hc_books.in_abs_library` flag + sync update + query

**Files:**
- Modify: `app/db.py` (migration list line ~134; new functions after `rate_hc_book`, line ~843; one line in `get_stats`, line ~779)
- Modify: `app/sync.py` (`sync_abs`, before its return)
- Test: `tests/test_want_to_read.py`

**Interfaces:**
- Produces:
  - `db.update_hc_book_abs(book_id: int, in_library: bool) -> None`
  - `db.get_want_to_read(q: str = "") -> list[sqlite3.Row]` — hc_books rows with `status_id = 1`, in-library first, then title; `q` filters title/author.
  - `db.get_stats(...)` result gains key `"want_to_read"` (int).
  - `sync_abs` also refreshes the flag for every want-to-read book.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_want_to_read.py`:

```python
def _seed_wtr(test_db):
    test_db.upsert_hc_book(201, "Project Hail Mary", "Andy Weir", None, None, None, 1, None)
    test_db.upsert_hc_book(202, "Piranesi", "Susanna Clarke", None, None, None, 1, None)
    test_db.upsert_hc_book(203, "Dune", "Frank Herbert", None, None, None, 3, 5.0)  # read, not WTR


def test_get_want_to_read_sorts_in_library_first(test_db):
    _seed_wtr(test_db)
    test_db.update_hc_book_abs(202, True)
    rows = test_db.get_want_to_read()
    assert [r["title"] for r in rows] == ["Piranesi", "Project Hail Mary"]
    assert rows[0]["in_abs_library"] == 1


def test_get_want_to_read_search(test_db):
    _seed_wtr(test_db)
    rows = test_db.get_want_to_read(q="weir")
    assert [r["title"] for r in rows] == ["Project Hail Mary"]


def test_stats_include_want_to_read_count(test_db):
    _seed_wtr(test_db)
    assert test_db.get_stats(1)["want_to_read"] == 2


def test_sync_abs_updates_wtr_flag(test_db, monkeypatch):
    import sync
    _seed_wtr(test_db)

    def fake_read_abs_db():
        titles = [sync._norm("Piranesi")]
        return titles, {}, {sync._norm("Piranesi"): "li_9"}, {"li_9": {}}
    monkeypatch.setattr(sync, "_read_abs_db", fake_read_abs_db)

    with test_db.db() as conn:
        recs = conn.execute(
            "SELECT id, title, hc_book_id, abs_library_item_id, in_abs_library "
            "FROM recommendations").fetchall()
    sync.sync_abs(recs)

    rows = test_db.get_want_to_read()
    flags = {r["title"]: r["in_abs_library"] for r in rows}
    assert flags["Piranesi"] == 1
    assert flags["Project Hail Mary"] == 0
```

> Note: if Plan 04 has NOT landed yet, the `recs` query in the last test can drop the `in_abs_library` column and `sync.sync_abs(recs)` returns an int — the test body above works unchanged either way because it ignores the return value.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_want_to_read.py -v`
Expected: FAIL (missing column/functions).

- [ ] **Step 3: Implement**

`app/db.py` — append to the migration list:

```python
            "ALTER TABLE hc_books ADD COLUMN in_abs_library INTEGER DEFAULT 0",
```

Add after `rate_hc_book` (line ~843):

```python
def update_hc_book_abs(book_id: int, in_library: bool):
    with db() as conn:
        conn.execute("UPDATE hc_books SET in_abs_library = ? WHERE id = ?",
                     (1 if in_library else 0, book_id))


def get_want_to_read(q: str = "") -> list[sqlite3.Row]:
    with db() as conn:
        where = "status_id = 1"
        params: list = []
        if q:
            where += " AND (lower(title) LIKE ? OR lower(COALESCE(author,'')) LIKE ?)"
            params += [f"%{q.lower()}%", f"%{q.lower()}%"]
        return conn.execute(f"""
            SELECT * FROM hc_books
            WHERE {where}
            ORDER BY in_abs_library DESC, title
        """, params).fetchall()
```

In `get_stats` (line ~779), add one entry to the returned dict (alongside the existing counts, same `conn`):

```python
        "want_to_read": conn.execute(
            "SELECT COUNT(*) FROM hc_books WHERE status_id = 1").fetchone()[0],
```

`app/sync.py` — in `sync_abs`, just before the function's `return`, add:

```python
    # Refresh the in-library flag for the Hardcover want-to-read shelf
    with db.db() as conn:
        wtr_books = conn.execute(
            "SELECT id, title FROM hc_books WHERE status_id = 1").fetchall()
    for book in wtr_books:
        db.update_hc_book_abs(book["id"], _fuzzy_match(book["title"], library_titles))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_want_to_read.py -v` — all PASS. Then full suite: `.venv/bin/pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/sync.py tests/test_want_to_read.py
git commit -m "feat: track ABS ownership of Hardcover want-to-read books"
```

---

### Task 2: Tab, grid partial, and queue action

**Files:**
- Modify: `app/main.py` (`STATUS_LABELS` line ~78; `recommendations_page` line ~118; new route `POST /wtr/{book_id}/queue` after it)
- Create: `app/templates/partials/wtr_grid.html`
- Modify: `app/templates/recommendations.html` (tab counts + grid include, lines 10-34)
- Test: `tests/test_want_to_read.py` (extend)

**Interfaces:**
- Consumes: `db.get_want_to_read(q)`, `db.upsert_recommendation(...) -> int`, `db.link_rec_to_hc(rec_id, hc_book_id)`, `db.add_to_queue(rec_id, profile_id)`, `push_queue_to_abs`.
- Produces: tab key `want_to_read` on `GET /?status=want_to_read` (full page and HX partial); `POST /wtr/{book_id}/queue`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_want_to_read.py`)

```python
def test_wtr_tab_renders(client, test_db):
    _seed_wtr(test_db)
    resp = client.get("/?status=want_to_read")
    assert resp.status_code == 200
    assert "Project Hail Mary" in resp.text


def test_wtr_queue_creates_linked_rec(client, test_db):
    _seed_wtr(test_db)
    resp = client.post("/wtr/201/queue", follow_redirects=False)
    assert resp.status_code == 303
    items = test_db.get_queue(1)
    assert any(i["title"] == "Project Hail Mary" for i in items)
    with test_db.db() as conn:
        rec = conn.execute(
            "SELECT hc_book_id FROM recommendations WHERE title = 'Project Hail Mary'"
        ).fetchone()
    assert rec["hc_book_id"] == 201
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_want_to_read.py -v` — new tests FAIL.

- [ ] **Step 3: Implement in `app/main.py`**

Add to `STATUS_LABELS` (between `in_library` and `archive`):

```python
    "want_to_read": "Want to Read",
```

Rework `recommendations_page` (line ~118):

```python
@app.get("/", response_class=HTMLResponse)
def recommendations_page(request: Request, status: str = "all", q: str = ""):
    profile_id = get_profile_id(request)
    if status == "want_to_read":
        books = db.get_want_to_read(q)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse("partials/wtr_grid.html",
                                              {"request": request, "books": books})
        return _tmpl(request, "recommendations.html",
                     recs=[], wtr_books=books, current_status=status, q=q,
                     status_labels=STATUS_LABELS)
    recs = db.get_recommendations(status, profile_id, q)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/rec_grid.html",
                                          {"request": request, "recs": recs})
    return _tmpl(request, "recommendations.html",
                 recs=recs, wtr_books=None, current_status=status, q=q,
                 status_labels=STATUS_LABELS)
```

Add the queue route below:

```python
@app.post("/wtr/{book_id}/queue")
def wtr_queue(book_id: int, request: Request, background_tasks: BackgroundTasks):
    profile_id = get_profile_id(request)
    with db.db() as conn:
        book = conn.execute("SELECT * FROM hc_books WHERE id = ?", (book_id,)).fetchone()
    if not book:
        return RedirectResponse("/?status=want_to_read", status_code=303)
    rec_id = db.upsert_recommendation(
        book["title"], book["author"], book["series"], "Book", "Unknown",
        "From your Hardcover Want-to-Read shelf.", source="want-to-read")
    db.link_rec_to_hc(rec_id, book_id)
    db.add_to_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    return RedirectResponse("/?status=want_to_read", status_code=303)
```

- [ ] **Step 4: Create `app/templates/partials/wtr_grid.html`**

```html
<div id="rec-results">
{% if books %}
<div class="rec-grid">
  {% for b in books %}
  <div class="rec-card">
    <div class="rec-cover-slot">
      <div class="rec-cover">
        {% if b.cover_url %}
          <img src="{{ b.cover_url }}" alt="" loading="lazy">
        {% else %}
          <span class="rec-cover-placeholder">📖</span>
        {% endif %}
      </div>
    </div>
    <div class="rec-body">
      <div>
        <h3 class="rec-title">{{ b.title }}</h3>
        <div class="rec-meta">
          <span class="rec-author">{{ b.author or "Unknown" }}</span>
          {% if b.series %}<span class="rec-series"> · {{ b.series }}</span>{% endif %}
        </div>
      </div>
      <div class="rec-badges">
        <span class="badge wtr-badge">🔖 Want to Read</span>
        {% if b.in_abs_library %}<span class="badge in-lib">In Library</span>{% endif %}
      </div>
      <div class="rec-actions">
        <form method="post" action="/wtr/{{ b.id }}/queue">
          <button class="btn btn-queue" type="submit">+ Queue</button>
        </form>
      </div>
    </div>
  </div>
  {% endfor %}
</div>
{% else %}
<p class="empty">Nothing on the Want-to-Read shelf{% if q %} matching your search{% endif %}. Sync to pull the latest from Hardcover.</p>
{% endif %}
</div>
```

> The wrapping `<div id="rec-results">` matches what the search box targets — check `partials/rec_grid.html` and mirror its wrapper exactly (add or omit the wrapper to match, so HX search swaps stay consistent).

- [ ] **Step 5: Wire the tab into `app/templates/recommendations.html`**

Add to the `tab_counts` dict (line 10):

```
  "want_to_read": stats.want_to_read,
```

Replace the grid include (line 34):

```html
{% if current_status == "want_to_read" %}
  {% include "partials/wtr_grid.html" with context %}
{% else %}
  {% include "partials/rec_grid.html" %}
{% endif %}
```

(`wtr_grid.html` reads `books` — pass it as `books=wtr_books` via a `{% set books = wtr_books %}` line above the include, or rename the context key consistently; keep template variable naming consistent between route and partial.)

- [ ] **Step 6: Run tests, verify in browser, commit**

Run: `.venv/bin/pytest -q` — all pass. In the browser: new tab shows shelf, in-library books first with badges; live search filters; + Queue moves a book into the queue (and the Queued tab).

```bash
git add app/main.py app/templates/partials/wtr_grid.html app/templates/recommendations.html tests/test_want_to_read.py
git commit -m "feat: Want to Read tab with ABS ownership badges and one-click queue"
```

---

### Task 3: Documentation

**Files:**
- Modify: `docs/user-guide.md` (Filter Tabs table), `docs/product-requirements.md` (endpoints table + Recommendations workflow)

- [ ] **Step 1: Update docs**

- `docs/user-guide.md` Filter Tabs table: add `| **Want to Read** | Your Hardcover want-to-read shelf — books you already own in ABS listed first |`.
- `docs/product-requirements.md`: endpoints table gains `POST /wtr/{id}/queue`; note in the Hardcover Integration section that WTR books are cross-referenced against ABS during sync.

- [ ] **Step 2: Commit**

```bash
git add docs/user-guide.md docs/product-requirements.md
git commit -m "docs: document Want to Read tab"
```
