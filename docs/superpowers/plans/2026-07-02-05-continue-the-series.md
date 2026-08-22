# Continue the Series Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/series` page listing every series where the reader has finished book N on Hardcover and book N+1 already sits in the Audiobookshelf library — with a one-click "+ Queue" that creates a rec and queues it.

**Architecture:** Sync gains a step that snapshots the ABS library's series index (series name, sequence, title, author, library item id) into a new `abs_series_items` table, rebuilt wholesale each sync. A pure-SQL query joins that against `hc_books` read history (status 3) to find the lowest unread next volume per series, excluding volumes already read/reading/DNF'd and series with a book currently in progress. Queuing reuses the existing rec machinery (`upsert_recommendation` + `add_to_queue` + ABS playlist push).

**Tech Stack:** SQLite CTE query, existing sync pipeline, Jinja2 template reusing `.rec-card` styles.

## Global Constraints

- Repo: `/mnt/c/Tools/bookclub`; test harness from Plan 01; run `.venv/bin/pytest -q` from repo root.
- **Ordering note:** if Plan 04 has landed, `run_full_sync` unpacks `sync_abs` as `abs_count, newly_available = sync_abs(recs)`; the hook added here goes after that line either way.
- ABS SQLite is mounted read-only; series data lives in `bookSeries` (`bookId`, `seriesId`, `sequence`) and `series` (`name`) — join via `books.id = bookSeries.bookId` and `libraryItems.mediaId = books.id` (see `docs/../../docs/abs_database.md` in the parent Tools repo, and ops-runbook Known Gotchas).
- Hardcover↔ABS series names are matched with `lower(trim(...))` equality — accepted limitation, documented in Task 4.
- Timestamps via `db._now()`. `recommendations` stays a global catalog; the new rec created on queue is visible to all profiles (pending for others) — that is correct behavior.
- Commits: conventional, no AI-attribution lines.

---

### Task 1: `abs_series_items` table + sync population

**Files:**
- Modify: `app/db.py` (add table to `init_db` executescript after `app_log`, line ~114; new function after `upsert_hc_book`, line ~613)
- Modify: `app/sync.py` (new `_read_abs_series()` after `_read_abs_db`, line ~270; hook in `run_full_sync` after the `sync_abs` call)
- Test: `tests/test_series.py`

**Interfaces:**
- Produces: table `abs_series_items(series, seq, title, author, library_item_id, synced_at)`; `db.replace_abs_series_items(items: list[dict]) -> None` (wholesale delete+insert); `sync._read_abs_series() -> list[dict]` with keys `series, seq, title, author, library_item_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_series.py`:

```python
def test_replace_abs_series_items_rebuilds_table(test_db):
    test_db.replace_abs_series_items([
        {"series": "The First Law", "seq": "1", "title": "The Blade Itself",
         "author": "Joe Abercrombie", "library_item_id": "li_1"},
    ])
    test_db.replace_abs_series_items([
        {"series": "The First Law", "seq": "2", "title": "Before They Are Hanged",
         "author": "Joe Abercrombie", "library_item_id": "li_2"},
    ])
    with test_db.db() as conn:
        rows = conn.execute("SELECT title FROM abs_series_items").fetchall()
    assert [r["title"] for r in rows] == ["Before They Are Hanged"]  # wholesale replace
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_series.py -v`
Expected: FAIL (`no such table` or missing attribute).

- [ ] **Step 3: Implement**

In `app/db.py`, add to the `init_db` executescript (after the `app_log` table):

```sql
        CREATE TABLE IF NOT EXISTS abs_series_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            series          TEXT NOT NULL,
            seq             TEXT,
            title           TEXT NOT NULL,
            author          TEXT,
            library_item_id TEXT NOT NULL,
            synced_at       DATETIME
        );
```

Add the function after `upsert_hc_book`:

```python
def replace_abs_series_items(items: list[dict]):
    """Wholesale rebuild of the ABS series index snapshot."""
    with db() as conn:
        conn.execute("DELETE FROM abs_series_items")
        conn.executemany(
            "INSERT INTO abs_series_items (series, seq, title, author, library_item_id, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(i["series"], i.get("seq"), i["title"], i.get("author"),
              i["library_item_id"], _now()) for i in items]
        )
```

In `app/sync.py`, add after `_read_abs_db` (line ~270):

```python
def _read_abs_series() -> list[dict]:
    """All ABS library items that belong to a series, with sequence numbers."""
    if not os.path.exists(ABS_DB_PATH):
        return []
    conn = sqlite3.connect(ABS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT li.id AS lib_id, b.title, s.name AS series, bs.sequence AS seq,
                   group_concat(a.name, ', ') AS author
            FROM books b
            JOIN libraryItems li ON li.mediaId = b.id
            JOIN bookSeries bs   ON bs.bookId = b.id
            JOIN series s        ON s.id = bs.seriesId
            LEFT JOIN bookAuthors ba ON ba.bookId = b.id
            LEFT JOIN authors a      ON a.id = ba.authorId
            GROUP BY li.id, s.id
        """).fetchall()
    finally:
        conn.close()
    return [{"series": r["series"], "seq": r["seq"], "title": r["title"],
             "author": r["author"], "library_item_id": r["lib_id"]} for r in rows]
```

In `run_full_sync`, immediately after the `sync_abs(...)` call and its log line, add:

```python
        series_items = _read_abs_series()
        if series_items:
            db.replace_abs_series_items(series_items)
            db.log("sync", f"ABS series index — {len(series_items)} entries")
```

- [ ] **Step 4: Run tests, commit**

Run: `.venv/bin/pytest -q` — all pass.

```bash
git add app/db.py app/sync.py tests/test_series.py
git commit -m "feat: snapshot ABS series index into abs_series_items during sync"
```

---

### Task 2: Continuation query

**Files:**
- Modify: `app/db.py` (new function after `replace_abs_series_items`)
- Test: `tests/test_series.py` (extend)

**Interfaces:**
- Consumes: `abs_series_items` (Task 1), `hc_books` (`status_id`: 1=Want, 2=Reading, 3=Read, 4=DNF; `series`, `series_pos`), `db.upsert_hc_book(book_id, title, author, series, series_pos, cover_url, status_id, rating)` for test seeding.
- Produces: `db.get_series_continuations() -> list[sqlite3.Row]` with columns `series, max_read, next_title, next_author, next_seq (REAL), library_item_id`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_series.py`)

```python
def _seed_first_law(test_db):
    # Read books 1-2 on Hardcover
    test_db.upsert_hc_book(101, "The Blade Itself", "Joe Abercrombie",
                           "The First Law", 1.0, None, 3, 5.0)
    test_db.upsert_hc_book(102, "Before They Are Hanged", "Joe Abercrombie",
                           "The First Law", 2.0, None, 3, 4.0)
    # ABS library has books 1-3
    test_db.replace_abs_series_items([
        {"series": "The First Law", "seq": "1", "title": "The Blade Itself",
         "author": "Joe Abercrombie", "library_item_id": "li_1"},
        {"series": "The First Law", "seq": "2", "title": "Before They Are Hanged",
         "author": "Joe Abercrombie", "library_item_id": "li_2"},
        {"series": "The First Law", "seq": "3", "title": "Last Argument of Kings",
         "author": "Joe Abercrombie", "library_item_id": "li_3"},
    ])


def test_continuation_finds_next_unread_volume(test_db):
    _seed_first_law(test_db)
    rows = test_db.get_series_continuations()
    assert len(rows) == 1
    assert rows[0]["next_title"] == "Last Argument of Kings"
    assert rows[0]["next_seq"] == 3.0
    assert rows[0]["max_read"] == 2.0
    assert rows[0]["library_item_id"] == "li_3"


def test_continuation_skips_series_in_progress(test_db):
    _seed_first_law(test_db)
    # currently reading book 3 -> series should disappear
    test_db.upsert_hc_book(103, "Last Argument of Kings", "Joe Abercrombie",
                           "The First Law", 3.0, None, 2, None)
    assert test_db.get_series_continuations() == []


def test_continuation_skips_dnf_next_volume(test_db):
    _seed_first_law(test_db)
    # DNF'd book 3 -> don't suggest it (and no book 4 exists in ABS)
    test_db.upsert_hc_book(103, "Last Argument of Kings", "Joe Abercrombie",
                           "The First Law", 3.0, None, 4, None)
    assert test_db.get_series_continuations() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_series.py -v`
Expected: new tests FAIL with `AttributeError: ... 'get_series_continuations'`

- [ ] **Step 3: Implement in `app/db.py`**

```python
def get_series_continuations() -> list[sqlite3.Row]:
    """Series where the reader finished book N (Hardcover) and the lowest
    unread volume N+ sits in the ABS library.

    Excludes: volumes already read/reading/DNF'd, and series with any book
    currently in progress. HC<->ABS series names match on lower(trim())."""
    with db() as conn:
        return conn.execute("""
            WITH read_series AS (
                SELECT lower(trim(series)) AS skey,
                       series               AS series_name,
                       MAX(series_pos)      AS max_read
                FROM hc_books
                WHERE status_id = 3 AND series IS NOT NULL AND series != ''
                  AND series_pos IS NOT NULL
                GROUP BY lower(trim(series))
            )
            SELECT rs.series_name      AS series,
                   rs.max_read         AS max_read,
                   a.title             AS next_title,
                   a.author            AS next_author,
                   CAST(a.seq AS REAL) AS next_seq,
                   a.library_item_id   AS library_item_id
            FROM read_series rs
            JOIN abs_series_items a
              ON lower(trim(a.series)) = rs.skey
            WHERE CAST(a.seq AS REAL) > rs.max_read
              AND CAST(a.seq AS REAL) = (
                  SELECT MIN(CAST(a2.seq AS REAL)) FROM abs_series_items a2
                  WHERE lower(trim(a2.series)) = rs.skey
                    AND CAST(a2.seq AS REAL) > rs.max_read)
              AND NOT EXISTS (
                  SELECT 1 FROM hc_books h2
                  WHERE lower(trim(h2.series)) = rs.skey
                    AND h2.series_pos = CAST(a.seq AS REAL)
                    AND h2.status_id IN (2, 3, 4))
              AND NOT EXISTS (
                  SELECT 1 FROM hc_books h3
                  WHERE lower(trim(h3.series)) = rs.skey AND h3.status_id = 2)
            ORDER BY rs.series_name
        """).fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_series.py -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_series.py
git commit -m "feat: series-continuation query (next unread volume in ABS library)"
```

---

### Task 3: Routes + page

**Files:**
- Modify: `app/main.py` (two routes, placed after the History section, line ~281)
- Create: `app/templates/series.html`
- Modify: `app/templates/base.html` (nav link, line 29)
- Test: `tests/test_series.py` (extend)

**Interfaces:**
- Consumes: `db.get_series_continuations()`, `db.upsert_recommendation(title, author, series, type_, audiobook_available, reason, source=..., tags=..., confidence=...) -> int`, `db.update_rec_abs_status(rec_id, in_library, progress, finished)`, `db.update_rec_abs_data(rec_id, *, library_item_id, description, duration, narrator, genres, series, series_seq, cover_url)`, `db.add_to_queue(rec_id, profile_id)`, `sync.push_queue_to_abs` (already imported in main).
- Produces: `GET /series` (page), `POST /series/queue` (form: `title, author, series, seq, library_item_id`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_series.py`)

```python
def test_series_page_lists_continuation(client, test_db):
    _seed_first_law(test_db)
    resp = client.get("/series")
    assert resp.status_code == 200
    assert "Last Argument of Kings" in resp.text


def test_series_queue_creates_rec_and_queues_it(client, test_db):
    _seed_first_law(test_db)
    resp = client.post("/series/queue", data={
        "title": "Last Argument of Kings", "author": "Joe Abercrombie",
        "series": "The First Law", "seq": "3.0", "library_item_id": "li_3",
    }, follow_redirects=False)
    assert resp.status_code == 303
    items = test_db.get_queue(1)
    assert any(i["title"] == "Last Argument of Kings" for i in items)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_series.py -v` — new tests FAIL with 404.

- [ ] **Step 3: Implement routes in `app/main.py`**

```python
# ---------------------------------------------------------------------------
# Continue the Series
# ---------------------------------------------------------------------------

@app.get("/series", response_class=HTMLResponse)
def series_page(request: Request):
    items = db.get_series_continuations()
    return _tmpl(request, "series.html", items=items)


@app.post("/series/queue")
def series_queue(request: Request, background_tasks: BackgroundTasks,
                 title: str = Form(...), author: str = Form(""),
                 series: str = Form(""), seq: str = Form(""),
                 library_item_id: str = Form("")):
    profile_id = get_profile_id(request)
    rec_id = db.upsert_recommendation(
        title, author or None, series or None, "Book", "Yes",
        f"Next in {series} — already in your Audiobookshelf library.",
        source="series")
    if library_item_id:
        db.update_rec_abs_status(rec_id, True, None, False)
        db.update_rec_abs_data(
            rec_id, library_item_id=library_item_id, description=None,
            duration=None, narrator=None, genres=None,
            series=series or None, series_seq=seq or None,
            cover_url=f"/abs/cover/{library_item_id}")
    db.add_to_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    return RedirectResponse("/series", status_code=303)
```

- [ ] **Step 4: Create `app/templates/series.html`**

```html
{% extends "base.html" %}
{% block title %}Series · Bookclub{% endblock %}
{% block content %}

<div class="page-header">
  <h1>Continue the Series</h1>
  <p class="subtitle">Series you've started where the next book is already in your Audiobookshelf library</p>
</div>

{% if items %}
<div class="rec-grid">
  {% for it in items %}
  <div class="rec-card">
    <div class="rec-cover-slot">
      <div class="rec-cover">
        <img src="/abs/cover/{{ it.library_item_id }}" alt="" loading="lazy">
      </div>
    </div>
    <div class="rec-body">
      <div>
        <h3 class="rec-title">{{ it.next_title }}</h3>
        <div class="rec-meta">
          <span class="rec-author">{{ it.next_author or "Unknown" }}</span>
          <span class="rec-series"> · {{ it.series }} #{{ "%g" | format(it.next_seq) }}</span>
        </div>
      </div>
      <div class="rec-badges">
        <span class="badge in-lib">In Library</span>
        <span class="badge" title="Highest series entry read on Hardcover">Read through #{{ "%g" | format(it.max_read) }}</span>
      </div>
      <div class="rec-actions">
        <form method="post" action="/series/queue">
          <input type="hidden" name="title" value="{{ it.next_title }}">
          <input type="hidden" name="author" value="{{ it.next_author or '' }}">
          <input type="hidden" name="series" value="{{ it.series }}">
          <input type="hidden" name="seq" value="{{ it.next_seq }}">
          <input type="hidden" name="library_item_id" value="{{ it.library_item_id }}">
          <button class="btn btn-queue" type="submit">+ Queue</button>
        </form>
      </div>
    </div>
  </div>
  {% endfor %}
</div>
{% else %}
<p class="empty">No series continuations found. Run a sync first — this view needs both your Hardcover history and the ABS library index.</p>
{% endif %}

{% endblock %}
```

- [ ] **Step 5: Add the nav link**

In `app/templates/base.html`, after the History link (line 29), add:

```html
      <a href="/series" {% if request.url.path == "/series" %}class="active"{% endif %}>Series</a>
```

- [ ] **Step 6: Run tests, verify in browser, commit**

Run: `.venv/bin/pytest -q` — all pass. Then run a real sync from the UI and open `http://localhost:8585/series`; covers load via the ABS cover proxy.

```bash
git add app/main.py app/templates/series.html app/templates/base.html tests/test_series.py
git commit -m "feat: Continue the Series page with one-click queue"
```

---

### Task 4: Documentation

**Files:**
- Modify: `docs/user-guide.md` (Navigation table + new section after Queue Page), `docs/product-requirements.md` (Core Requirements + API endpoints table), `docs/architecture.md` (schema section: `abs_series_items`; request flow: `/series`)

**Interfaces:** none.

- [ ] **Step 1: Update docs**

- `docs/user-guide.md` navigation table: add `| **Series** | Next unread volumes of series you've started, already in your library |`, plus a short section: what qualifies (read book N on Hardcover, N+1 in ABS), the exclusions (in-progress series, read/DNF volumes), and the known limitation that Hardcover and ABS must use the same series name (case-insensitive) to match.
- `docs/product-requirements.md`: add a Core Requirement "11. Series continuation" describing the feature and the `lower(trim())` name-matching limitation; add `GET /series` and `POST /series/queue` to the endpoints table.
- `docs/architecture.md`: add `abs_series_items` to the schema section and `/series` to the request-flow diagram.

- [ ] **Step 2: Commit**

```bash
git add docs/user-guide.md docs/product-requirements.md docs/architecture.md
git commit -m "docs: document Continue the Series feature"
```
