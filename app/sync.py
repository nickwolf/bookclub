"""
Sync logic:
  1. Pull all user books from Hardcover (paginated)
  2. Read ABS SQLite DB for library items + listen history
  3. Cross-reference recommendations against both sources
  4. Seed recommendations from the hardcoded list if DB is empty
"""

import json
import time
import os
import difflib
import sqlite3
import threading
import httpx

import db
from textnorm import _norm, author_surnames, full_title_key, title_keys

HARDCOVER_API = "https://api.hardcover.app/v1/graphql"
ABS_DB_PATH          = os.environ.get("ABS_DB_PATH", "/abs_config/absdatabase.sqlite")
ABS_URL              = os.environ.get("ABS_URL", "")
ABS_TOKEN            = os.environ.get("ABS_TOKEN", "")
ABS_PLAYLIST_ID      = os.environ.get("ABS_PLAYLIST_ID", "")
HARDCOVER_TOKEN      = os.environ.get("HARDCOVER_TOKEN", "")
ABS_PICKS_PLAYLIST_NAME = "Bookclub Picks"
ABS_PICKS_DESCRIPTION = "AI-curated recommendations already in your library, ordered by match confidence."

HC_QUERY = """
query GetUserBooks($limit: Int!, $after: Int!) {
  me {
    user_books(limit: $limit, where: {id: {_gt: $after}}, order_by: {id: asc}) {
      id
      book {
        id
        title
        image { url }
        contributions { author { name } }
        book_series { series { name } position }
      }
      status_id
      rating
    }
  }
}
"""

HC_COUNT_QUERY = """
query CountUserBooks {
  me {
    user_books_aggregate {
      aggregate { count }
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Seed recommendations — same list as the original CSV script, used only once
# ---------------------------------------------------------------------------

SEED_RECOMMENDATIONS = [
    ("He Who Fights With Monsters", "Jason Cheyne", "He Who Fights With Monsters", "Series", "Yes",
     "LitRPG — long-running series with system, portals, and dungeon-crawling; very similar energy to DCC and Primal Hunter"),
    ("Defiance of the Fall", "JF Brink", "Defiance of the Fall", "Series", "Yes",
     "LitRPG/Progression — system apocalypse, very long series, web-serial origin like Primal Hunter"),
    ("Life Reset", "Shemer Kuznits", "Life Reset", "Series", "Yes",
     "LitRPG — trapped-in-game with clever monster-mob mechanics; stands out in the genre"),
    ("Super Powereds", "Drew Hayes", "Super Powereds", "Series", "Yes",
     "Progression/superhero — college setting, power growth and training arcs"),
    ("Dungeon Diving 101", "Rook", "Dungeon Diving 101", "Series", "Yes",
     "LitRPG — dungeon-focused with light tones; Royal Road origin"),
    ("Randidly Ghosthound", "puddles4263", "Randidly Ghosthound", "Series", "Partial",
     "LitRPG/Progression — one of the OG long-form web serials; system apocalypse with deep class building"),
    ("Sufficiently Advanced Magic", "Andrew Rowe", "Arcane Ascension", "Series", "Yes",
     "Progression fantasy — you've read Rowe's standalone; this is his main series with tower-climbing and a complex magic system"),
    ("Mother of Learning", "Domagoj Kurmaic", None, "Book", "Yes",
     "Progression fantasy — time-loop magic school; one of the best-rated web serials ever adapted to audio"),
    ("Forge of Destiny", "Yrsillar", "Forge of Destiny", "Series", "Yes",
     "Xianxia/Cultivation — female protagonist, slower slice-of-life pacing; complements Beware of Chicken"),
    ("A Thousand Li", "Tao Wong", "A Thousand Li", "Series", "Yes",
     "Cultivation/Xianxia — you've read Tao Wong's cozy fantasy; this is his xianxia series"),
    ("The Long Way to a Small, Angry Planet", "Becky Chambers", "Wayfarers", "Series", "Yes",
     "Cozy space opera — slice-of-life crew on a tunneling ship; same warm vibe as Murderbot Diaries"),
    ("Wool", "Hugh Howey", "Silo", "Series", "Yes",
     "Hard SF/post-apocalyptic — underground silo society; gripping mystery box structure"),
    ("Old Man's War", "John Scalzi", "Old Man's War", "Series", "Yes",
     "Military SF — fast, witty, and action-packed; pairs well with Expeditionary Force"),
    ("Project Hail Mary", "Andy Weir", None, "Book", "Yes",
     "Hard SF — you've read The Martian; this is Weir's best work, solo astronaut first-contact mystery"),
    ("Spin", "Robert Charles Wilson", "Spin Trilogy", "Series", "Yes",
     "SF — you finished Wilson's Axis/Vortex; Spin is the first and best book of that trilogy"),
    ("Mistborn: The Final Empire", "Brandon Sanderson", "Mistborn", "Series", "Yes",
     "Epic fantasy — hard magic system, heist structure, underdog revolution; Sanderson's most accessible entry"),
    ("The Stormlight Archive", "Brandon Sanderson", "The Stormlight Archive", "Series", "Yes",
     "Epic fantasy — massive progression arcs (Knights Radiant powers), ~45hrs per book"),
    ("The Dresden Files", "Jim Butcher", "The Dresden Files", "Series", "Yes",
     "Urban fantasy — you have Codex Alera; Dresden is Butcher's other series: wizard detective in Chicago"),
    ("The Malazan Book of the Fallen", "Steven Erikson", "Malazan Book of the Fallen", "Series", "Yes",
     "Epic fantasy — the densest most ambitious fantasy series ever written; armies, gods, massive scope"),
    ("Piranesi", "Susanna Clarke", None, "Book", "Yes",
     "Literary fantasy — labyrinthine house with tides and statues; short, beautiful, mysterious"),
    ("Tress of the Emerald Sea", "Brandon Sanderson", "The Cosmere", "Book", "Yes",
     "Fantasy/adventure — Sanderson's lightest book; fairytale structure with sharp wit like Pratchett"),
    ("The Goblin Emperor", "Katherine Addison", None, "Book", "Yes",
     "Cozy fantasy — accidental emperor navigating court politics with pure kindness; no grimdark"),
    ("Among Thieves", "M.J. Kuhn", "The Thieves of Fate", "Series", "Yes",
     "Fantasy heist — ensemble cast of criminals; similar vibe to The Palace Job"),
    ("Legends & Lattes", "Travis Baldree", None, "Book", "Yes",
     "Cozy fantasy — you have Brigands & Breadknives; this is Baldree's debut novel that started the cozy wave"),
    ("A Psalm for the Wild-Built", "Becky Chambers", "Monk & Robot", "Series", "Yes",
     "Cozy SF — tea monk travels in a world where robots achieved consciousness; short and meditative"),
    ("Bookshops & Bonedust", "Travis Baldree", None, "Book", "Yes",
     "Cozy fantasy prequel to Legends & Lattes; young orc warrior recuperating in a small town bookshop"),
    ("The Wandering Inn", "pirateaba", "The Wandering Inn", "Series", "Yes",
     "Web serial / LitRPG — you have Lady of Fire (a TWI side story); the main series is the longest ongoing serial, officially narrated"),
    ("Worm", "Wildbow", None, "Book", "Partial",
     "Web serial / superhero — dark deconstruction of cape fiction; fan-narrated audiobook exists"),
    ("Four Thousand Weeks", "Oliver Burkeman", None, "Book", "Yes",
     "Non-fiction — anti-productivity productivity book; argues for embracing finitude"),
    ("The WEIRDest People in the World", "Joseph Henrich", None, "Book", "Yes",
     "Non-fiction / history — how Western psychology became the global default; pairs with Dawn of Everything"),
    ("Sapiens", "Yuval Noah Harari", None, "Book", "Yes",
     "Non-fiction / history — sweeping history of humanity; good if you liked A Distant Mirror and People's History"),
    ("The Riyria Revelations", "Michael J. Sullivan", "The Riyria Revelations", "Series", "Yes",
     "Epic fantasy — two classic thieves on impossible jobs; tight plotting, great banter, very bingeable"),
]


# ---------------------------------------------------------------------------
# Normalisation helpers (shared with ABS cross-reference)
# ---------------------------------------------------------------------------

def _fuzzy_match(needle: str, haystack: list[str], cutoff: float = 0.72) -> bool:
    norm = _norm(needle)
    short = " ".join(norm.split()[:4])
    for candidate in [norm, short]:
        if difflib.get_close_matches(candidate, haystack, n=1, cutoff=cutoff):
            return True
    return False


# ---------------------------------------------------------------------------
# Hardcover sync
# ---------------------------------------------------------------------------

def _hc_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {HARDCOVER_TOKEN}",
    }


def _hc_post(client: httpx.Client, payload: dict, attempts: int = 5,
             max_wait: float = 60) -> httpx.Response:
    """POST to Hardcover, backing off on 429 (the token is rate limited and shared)."""
    for attempt in range(attempts):
        resp = client.post(HARDCOVER_API, headers=_hc_headers(), json=payload)
        if resp.status_code != 429 or attempt == attempts - 1:
            resp.raise_for_status()
            return resp
        try:
            wait = float(resp.headers.get("retry-after", ""))
        except ValueError:
            wait = 2 ** (attempt + 2)
        db.log("sync", f"Hardcover rate limited, retrying in {wait:.0f}s", level="warning")
        _sleep(min(wait, max_wait))


def _sleep(seconds: float):
    time.sleep(seconds)


HC_SEARCH_QUERY = """
query Search($q: String!, $t: String!, $n: Int!) {
  search(query: $q, query_type: $t, per_page: $n, page: 1) { results }
}
"""
HC_SEARCH_HITS = 5
HC_TITLE_ONLY_HITS = 10
HC_TITLE_CUTOFF = 0.9


def _titles_agree(a: set[str], b: set[str]) -> bool:
    if a & b:
        return True
    return any(difflib.SequenceMatcher(None, x, y).ratio() >= HC_TITLE_CUTOFF
               for x in a for y in b)


def _hit_qualifies(doc_title: str, doc_authors: str, title: str, author: str | None) -> bool:
    """Search is fuzzy and always returns hits, so require title and author to agree."""
    want = title_keys(title)
    have = title_keys(doc_title)
    wanted = author_surnames(author)
    if not wanted:
        return bool(want & have)
    return ((_titles_agree(want, have) or _series_prefix_match(doc_title, title))
            and bool(wanted & author_surnames(doc_authors)))


def _series_prefix_match(doc_title: str, title: str) -> bool:
    """'Mistborn' for 'Mistborn: The Final Empire', a shortening title_keys can't see."""
    key = full_title_key(title)
    return bool(key) and ":" in doc_title and full_title_key(doc_title.split(":")[0]) == key


def _hc_search(client: httpx.Client, title: str, author: str | None, kind: str,
               hits: int = HC_SEARCH_HITS, **post_kw) -> list[dict]:
    """Search documents of the given query type ("Book" or "Series"). Raises on API errors."""
    resp = _hc_post(client, {"query": HC_SEARCH_QUERY,
                             "variables": {"q": f"{title} {author or ''}".strip(),
                                           "t": kind, "n": hits}}, **post_kw)
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Hardcover search error: {body['errors']}")
    results = ((body.get("data") or {}).get("search") or {}).get("results") or {}
    return [h.get("document") or {} for h in results.get("hits") or []]


def _closeness(doc_title: str, title: str) -> tuple[bool, bool, float]:
    """Rank key: exact full-title match, then title-key agreement ranked by similarity (so a
    parenthetical edition ranks lower). Series-prefix-only matches tie, leaving popularity to decide."""
    exact = full_title_key(doc_title) == full_title_key(title)
    if not _titles_agree(title_keys(doc_title), title_keys(title)):
        return exact, False, 0.0
    return exact, True, difflib.SequenceMatcher(None, _norm(doc_title), _norm(title)).ratio()


def search_hc_book(client: httpx.Client, title: str, author: str | None,
                   title_only: bool = False, **post_kw) -> dict | None:
    """Best Hardcover book match for a title and author, or None. Raises on API errors.

    title_only leaves the author out of the query (and widens it) but still requires the
    author to agree on the hits.
    """
    hits = _hc_search(client, title, None if title_only else author, "Book",
                      HC_TITLE_ONLY_HITS if title_only else HC_SEARCH_HITS, **post_kw)
    docs = [d for d in hits
            if d.get("id") and _hit_qualifies(d.get("title") or "",
                                              ", ".join(d.get("author_names") or []),
                                              title, author)]
    if not docs:
        return None
    best = max(docs, key=lambda d: (*_closeness(d.get("title") or "", title),
                                    d.get("users_count") or 0))
    featured = best.get("featured_series") or {}
    return {
        "hardcover_id": int(best["id"]),
        "title": best.get("title") or title,
        "author": ", ".join(best.get("author_names") or []) or author,
        "cover_url": (best.get("image") or {}).get("url"),
        "series": (featured.get("series") or {}).get("name"),
        "series_pos": featured.get("position"),
    }


def search_hc_series(client: httpx.Client, title: str, author: str | None,
                     **post_kw) -> dict | None:
    """Best Hardcover series match (name and author only), or None. Raises on API errors."""
    docs = [d for d in _hc_search(client, title, author, "Series", **post_kw)
            if d.get("id") and _hit_qualifies(d.get("name") or "", d.get("author_name") or "",
                                              title, author)]
    if not docs:
        return None
    best = max(docs, key=lambda d: (*_closeness(d.get("name") or "", title),
                                    d.get("readers_count") or 0))
    return {"title": best["name"], "author": best.get("author_name") or author}


def _hc_user_book_count(client: httpx.Client) -> int | None:
    """Total user_books rows on Hardcover, or None if the count can't be read."""
    try:
        data = _hc_post(client, {"query": HC_COUNT_QUERY}).json()
        count = data["data"]["me"][0]["user_books_aggregate"]["aggregate"]["count"]
        return count if isinstance(count, int) else None
    except Exception as e:
        db.log("sync", f"Hardcover book count unavailable: {e}", level="warning")
        return None


def _prune_removed(client: httpx.Client, seen_books: set[int], seen_rows: set[int]):
    """Delete hc_books rows missing from a sync that provably saw every user book."""
    if not seen_books:
        db.log("sync", "Hardcover returned zero books, skipping prune of removed books", level="warning")
        return
    expected = _hc_user_book_count(client)
    if expected is None:
        db.log("sync", "Skipping prune of removed books, could not confirm Hardcover book count",
               level="warning")
        return
    if len(seen_rows) != expected:
        db.log("sync", f"Hardcover sync saw {len(seen_rows)} of {expected} books, "
                       "skipping prune until a sync sees them all", level="warning")
        return
    removed = db.prune_hc_books(seen_books)
    if removed:
        db.log("sync", f"Removed {removed} books no longer on Hardcover")


def sync_hardcover() -> int:
    """Pull all user books from Hardcover and upsert into hc_books. Returns count synced.

    Pages by user_book id (keyset), so deletions mid-sync can't shift rows out of view. Books no
    longer on Hardcover are pruned only if the rows seen match Hardcover's own count.
    """
    total = 0
    seen_books: set[int] = set()
    seen_rows: set[int] = set()
    after = 0
    limit = 100

    with httpx.Client(timeout=30) as client:
        while True:
            resp = _hc_post(client, {"query": HC_QUERY,
                                     "variables": {"limit": limit, "after": after}})
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"Hardcover API error: {data['errors']}")
            me = data.get("data", {}).get("me", [])
            if not me:
                raise RuntimeError("Hardcover API returned no user data — check token")
            user_books = me[0].get("user_books")
            if not isinstance(user_books, list):
                raise RuntimeError("Hardcover API returned no user_books list")
            if not user_books:
                break

            for ub in user_books:
                book = ub["book"]
                bid = book["id"]
                title = book["title"]
                author = ", ".join(
                    c["author"]["name"] for c in (book.get("contributions") or [])
                )
                series_info = book.get("book_series") or []
                series = series_info[0]["series"]["name"] if series_info else None
                series_pos = series_info[0].get("position") if series_info else None
                cover_url = (book.get("image") or {}).get("url")
                status_id = ub["status_id"]
                rating = ub.get("rating")

                db.upsert_hc_book(bid, title, author, series, series_pos, cover_url, status_id, rating)
                seen_books.add(bid)
                seen_rows.add(ub["id"])
                total += 1

            after = user_books[-1]["id"]

        _prune_removed(client, seen_books, seen_rows)
    return total


# ---------------------------------------------------------------------------
# ABS library / listen-history sync
# ---------------------------------------------------------------------------

def _parse_json_list(raw: str | None) -> str | None:
    """Parse a JSON array string like '["Alice","Bob"]' to 'Alice, Bob'."""
    if not raw:
        return None
    try:
        items = json.loads(raw)
        return ", ".join(str(i) for i in items) if items else None
    except Exception:
        return raw


def _read_abs_db() -> tuple[list[str], dict[str, tuple[float, bool]], dict[str, str], dict[str, dict]]:
    """
    Returns:
      library_titles  — list of normalised book titles in the ABS library
      progress_map    — {normalised_title: (progress_pct, is_finished)}
      item_id_map     — {normalised_title: libraryItemId}
      book_details    — {libraryItemId: {description, duration, narrator, genres, series, series_seq}}
    """
    if not os.path.exists(ABS_DB_PATH):
        return [], {}, {}, {}

    conn = sqlite3.connect(ABS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT li.id as libraryItemId, b.title, b.description, b.duration as bookDuration,
                   b.narrators, b.genres, mp.currentTime, mp.duration, mp.isFinished,
                   s.name as series_name, bs.sequence as series_seq,
                   group_concat(a.name, ', ') as author
            FROM   libraryItems li
            JOIN   books b ON li.mediaId = b.id
            LEFT JOIN mediaProgresses mp
                   ON json_extract(mp.extraData, '$.libraryItemId') = li.id
            LEFT JOIN bookSeries bs ON bs.bookId = b.id
            LEFT JOIN series s ON s.id = bs.seriesId
            LEFT JOIN bookAuthors ba ON ba.bookId = b.id
            LEFT JOIN authors a ON a.id = ba.authorId
            GROUP BY li.id
        """).fetchall()
    finally:
        conn.close()

    library_titles = []
    progress_map = {}
    item_id_map = {}
    book_details = {}

    for row in rows:
        norm = _norm(row["title"])
        if norm not in library_titles:
            library_titles.append(norm)
            item_id_map[norm] = row["libraryItemId"]
        if row["currentTime"] is not None and row["duration"] and row["duration"] > 0:
            pct = row["currentTime"] / row["duration"]
            finished = bool(row["isFinished"])
            existing = progress_map.get(norm)
            if existing is None or pct > existing[0]:
                progress_map[norm] = (pct, finished)
        book_details[row["libraryItemId"]] = {
            "description": row["description"],
            "duration":    row["bookDuration"],
            "narrator":    _parse_json_list(row["narrators"]),
            "genres":      _parse_json_list(row["genres"]),
            "series":      row["series_name"],
            "series_seq":  row["series_seq"],
        }

    return library_titles, progress_map, item_id_map, book_details


def sync_abs(rec_rows: list) -> int:
    """Cross-reference all recommendations against the ABS DB. Returns count updated."""
    library_titles, progress_map, item_id_map, book_details = _read_abs_db()
    if not library_titles:
        return 0

    updated = 0
    for rec in rec_rows:
        in_lib = _fuzzy_match(rec["title"], library_titles)
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

    return updated


# ---------------------------------------------------------------------------
# ABS playlist bidirectional sync
# ---------------------------------------------------------------------------

def read_abs_playlist() -> list[dict]:
    """
    Read the ABS Reading List playlist from SQLite in order, including rich book data.
    """
    if not os.path.exists(ABS_DB_PATH) or not ABS_PLAYLIST_ID:
        return []

    conn = sqlite3.connect(ABS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT pmi."order", li.id as libraryItemId, b.title,
                   b.description, b.duration as bookDuration,
                   b.narrators, b.genres,
                   group_concat(a.name, ', ') as author,
                   s.name as series_name, bs.sequence as series_seq
            FROM playlistMediaItems pmi
            JOIN books b ON pmi.mediaItemId = b.id
            JOIN libraryItems li ON li.mediaId = b.id
            LEFT JOIN bookAuthors ba ON ba.bookId = b.id
            LEFT JOIN authors a ON a.id = ba.authorId
            LEFT JOIN bookSeries bs ON bs.bookId = b.id
            LEFT JOIN series s ON s.id = bs.seriesId
            WHERE pmi.playlistId = ?
            GROUP BY pmi.id
            ORDER BY pmi."order"
        """, (ABS_PLAYLIST_ID,)).fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        results.append({
            "order":        r["order"],
            "libraryItemId": r["libraryItemId"],
            "title":        r["title"],
            "author":       r["author"],
            "description":  r["description"],
            "duration":     r["bookDuration"],
            "narrator":     _parse_json_list(r["narrators"]),
            "genres":       _parse_json_list(r["genres"]),
            "series":       r["series_name"],
            "series_seq":   r["series_seq"],
        })
    return results


def sync_abs_playlist(profile_id: int = 1) -> int:
    """
    Reconcile the Bookclub queue from the ABS Reading List playlist.
    ABS order is authoritative. Bookclub-only queue items are appended after.
    Returns number of queue items after reconciliation.
    """
    abs_items = read_abs_playlist()
    if not abs_items:
        return 0

    # Save any Bookclub-only queue items (no ABS library item ID) before wiping
    with db.db() as conn:
        bookclub_only = conn.execute("""
            SELECT q.rec_id, q.position
            FROM queue q
            JOIN recommendations r ON r.id = q.rec_id
            WHERE q.profile_id = ? AND r.abs_library_item_id IS NULL
            ORDER BY q.position
        """, (profile_id,)).fetchall()

    db.wipe_queue(profile_id)

    # Rebuild queue from ABS playlist in order
    for i, item in enumerate(abs_items, 1):
        db.upsert_abs_playlist_item(
            profile_id, item["title"], item["author"],
            item["libraryItemId"], i,
            description=item.get("description"),
            duration=item.get("duration"),
            narrator=item.get("narrator"),
            genres=item.get("genres"),
            series=item.get("series"),
            series_seq=item.get("series_seq"),
        )

    # Re-append Bookclub-only items after the ABS items
    offset = len(abs_items)
    for j, row in enumerate(bookclub_only, 1):
        with db.db() as conn:
            conn.execute("""
                INSERT INTO queue (rec_id, profile_id, position)
                VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING
            """, (row["rec_id"], profile_id, offset + j))
            conn.execute("""
                INSERT INTO rec_interactions (profile_id, rec_id, user_status, updated_at)
                VALUES (?, ?, 'queued', ?)
                ON CONFLICT(profile_id, rec_id) DO UPDATE SET
                    user_status = 'queued', updated_at = excluded.updated_at
            """, (profile_id, row["rec_id"], db._now()))

    return offset + len(bookclub_only)


def _abs_http() -> httpx.Client:
    """HTTP client factory for ABS calls (tests inject a MockTransport here)."""
    return httpx.Client(timeout=10)


def _abs_filter_existing(ids: list[str]) -> list[str]:
    """Drop ids that no longer exist in ABS (batch/add 400s on unknown ids). Keeps all if the DB is unreadable."""
    ids = list(ids)
    if not ids or not os.path.exists(ABS_DB_PATH):
        return ids
    try:
        conn = sqlite3.connect(f"file:{ABS_DB_PATH}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(ids))
            found = {r[0] for r in conn.execute(
                f"SELECT id FROM libraryItems WHERE id IN ({marks})", ids)}
        finally:
            conn.close()
    except sqlite3.Error as e:
        db.log("abs", f"Could not check library items in ABS DB: {e}", level="warning")
        return ids
    stale = [i for i in ids if i not in found]
    if stale:
        db.log("abs", f"Skipping {len(stale)} item(s) no longer in ABS: {', '.join(stale)}",
               level="warning")
    return [i for i in ids if i in found]


def _abs_items(ids: list[str]) -> list[dict]:
    return [{"libraryItemId": i, "episodeId": None} for i in ids]


def _abs_set_playlist_items(abs_url: str, token: str, playlist_id: str,
                            desired_ids: list[str], allow_empty: bool) -> str:
    """
    Make an ABS playlist hold exactly desired_ids, in that order.
    PATCH only reorders, so adds and removes go through batch/add and batch/remove.
    Returns "missing", "skipped", "unchanged", "updated" or "deleted".
    Network and HTTP errors propagate to the caller.
    """
    desired = list(dict.fromkeys(desired_ids))
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{abs_url}/api/playlists/{playlist_id}"

    with _abs_http() as client:
        resp = client.get(base, headers=headers)
        if resp.status_code == 404:
            return "missing"
        resp.raise_for_status()
        current = [i["libraryItemId"] for i in resp.json().get("items", [])]
        have = set(current)

        # Ids already in the playlist are kept as is; only new ones need to exist
        new = _abs_filter_existing([i for i in desired if i not in have])
        desired = [i for i in desired if i in have or i in new]

        if not desired and not allow_empty:
            db.log("abs", f"Refusing to empty ABS playlist {playlist_id}: ABS would delete it",
                   level="warning")
            return "skipped"

        wanted = set(desired)
        to_add = [i for i in desired if i not in have]
        to_remove = [i for i in current if i not in wanted]

        if to_add:
            r = client.post(f"{base}/batch/add", headers=headers, json={"items": _abs_items(to_add)})
            r.raise_for_status()
        if to_remove:
            r = client.post(f"{base}/batch/remove", headers=headers, json={"items": _abs_items(to_remove)})
            r.raise_for_status()
            if not desired:
                return "deleted"

        # Adds append, removes keep relative order: this is the order ABS now holds
        resulting = [i for i in current if i in wanted] + to_add
        if resulting != desired:
            r = client.patch(base, headers=headers, json={"items": _abs_items(desired)})
            r.raise_for_status()
            return "updated"
        return "updated" if (to_add or to_remove) else "unchanged"


def _abs_apply_queue_change(abs_url: str, token: str, playlist_id: str, add: list[str],
                            remove: list[str], local_order: list[str] | None) -> str:
    """
    Apply one queue action to an ABS playlist without touching items bookclub did not change.
    local_order, when given, reorders: items in both lists follow it, ABS-only items keep
    their ABS order after them. Returns "missing", "skipped", "unchanged" or "updated".
    """
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{abs_url}/api/playlists/{playlist_id}"

    with _abs_http() as client:
        resp = client.get(base, headers=headers)
        if resp.status_code == 404:
            return "missing"
        resp.raise_for_status()
        current = [i["libraryItemId"] for i in resp.json().get("items", [])]
        have = set(current)

        to_add = _abs_filter_existing([i for i in add if i not in have])
        to_remove = [i for i in remove if i in have and i not in to_add]
        if to_remove and not [i for i in current if i not in to_remove] and not to_add:
            db.log("abs", f"Not removing the last item from ABS playlist {playlist_id}: "
                          "ABS would delete it", level="warning")
            to_remove = []

        if to_add:
            r = client.post(f"{base}/batch/add", headers=headers, json={"items": _abs_items(to_add)})
            r.raise_for_status()
        if to_remove:
            r = client.post(f"{base}/batch/remove", headers=headers, json={"items": _abs_items(to_remove)})
            r.raise_for_status()
        resulting = [i for i in current if i not in to_remove] + to_add
        changed = bool(to_add or to_remove)

        if local_order is not None:
            present = set(resulting)
            first = [i for i in dict.fromkeys(local_order) if i in present]
            firstset = set(first)
            target = first + [i for i in resulting if i not in firstset]
            if target != resulting:
                r = client.patch(base, headers=headers, json={"items": _abs_items(target)})
                r.raise_for_status()
                changed = True
        return "updated" if changed else "unchanged"


_push_lock = threading.Lock()


def push_queue_to_abs(profile_id: int = 1, add=(), remove=(), reorder: bool = False) -> bool:
    """
    Push one queue action to the ABS Reading List playlist.
    add and remove are recommendation ids; reorder applies the local order.
    Only touches the items named, so edits made in ABS since the last pull survive.
    Runs under a lock and reads the local queue at run time, not when it was queued.
    Returns True on success.
    """
    if not ABS_URL or not ABS_TOKEN or not ABS_PLAYLIST_ID:
        return False

    with _push_lock:
        queue_rows = db.get_queue_abs_items(profile_id)
        in_queue = {row["rec_id"] for row in queue_rows}
        local_order = [row["abs_library_item_id"] for row in queue_rows]
        # An add since undone, or a remove since redone, is no longer ours to push
        add_ids = _abs_item_ids_for_recs([r for r in add if r in in_queue])
        # Another queued rec may share the ABS item, so keep anything still queued
        remove_ids = [i for i in _abs_item_ids_for_recs([r for r in remove if r not in in_queue])
                      if i not in set(local_order)]
        if not (add_ids or remove_ids or reorder):
            return False
        try:
            status = _abs_apply_queue_change(ABS_URL, ABS_TOKEN, ABS_PLAYLIST_ID, add_ids,
                                             remove_ids, local_order if reorder else None)
        except Exception as e:
            db.log("abs", f"Failed to push queue to ABS: {e}", level="error")
            return False
    if status == "missing":
        db.log("abs", f"ABS playlist {ABS_PLAYLIST_ID} not found, queue not pushed", level="error")
        return False
    db.log("abs", f"Pushed queue change to ABS playlist (+{len(add_ids)} -{len(remove_ids)}"
                  f"{' reorder' if reorder else ''}, {status})")
    return True


def _abs_item_ids_for_recs(rec_ids: list[int]) -> list[str]:
    """ABS library item ids for recommendation ids, in the given order, skipping unlinked ones."""
    ids = []
    with db.db() as conn:
        for rid in rec_ids:
            row = conn.execute("SELECT abs_library_item_id FROM recommendations WHERE id = ?",
                               (rid,)).fetchone()
            if row and row["abs_library_item_id"]:
                ids.append(row["abs_library_item_id"])
    return ids


# ---------------------------------------------------------------------------
# Link recommendations ↔ Hardcover books
# ---------------------------------------------------------------------------

def link_recs_to_hc(rec_rows: list):
    """Link each recommendation to a Hardcover shelf book: by id when known, else fuzzy title."""
    with db.db() as conn:
        hc_books = conn.execute(
            "SELECT id, lower(title) as norm_title FROM hc_books"
        ).fetchall()
    shelf_ids = {row["id"] for row in hc_books}

    hc_norm_map = {row["norm_title"]: row["id"] for row in hc_books}
    hc_norms = list(hc_norm_map.keys())

    for rec in rec_rows:
        if rec["hc_book_id"]:
            continue
        if rec["hardcover_id"] is not None:
            if rec["hardcover_id"] in shelf_ids:
                db.link_rec_to_hc(rec["id"], rec["hardcover_id"])
            continue
        norm = _norm(rec["title"])
        matches = difflib.get_close_matches(norm, hc_norms, n=1, cutoff=0.8)
        if matches:
            hc_id = hc_norm_map[matches[0]]
            db.link_rec_to_hc(rec["id"], hc_id)


# ---------------------------------------------------------------------------
# Seed on first run
# ---------------------------------------------------------------------------

def seed_if_empty():
    with db.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    if count > 0:
        return

    for title, author, series, type_, audio_avail, reason in SEED_RECOMMENDATIONS:
        db.upsert_recommendation(title, author, series, type_, audio_avail, reason)


# ---------------------------------------------------------------------------
# Bookclub Picks playlist
# ---------------------------------------------------------------------------

def _abs_library_id_for_item(item_id: str) -> str | None:
    """Library a library item belongs to, read from the ABS SQLite."""
    if not os.path.exists(ABS_DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{ABS_DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT libraryId FROM libraryItems WHERE id = ?", (item_id,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error as e:
        db.log("abs", f"Could not read library id from ABS DB: {e}", level="warning")
        return None
    finally:
        conn.close()


def _find_picks_playlist(client: httpx.Client, abs_url: str, headers: dict) -> str | None:
    """Id of this token's user's 'Bookclub Picks' playlist that bookclub created, if any."""
    resp = client.get(f"{abs_url}/api/playlists", headers=headers)
    resp.raise_for_status()
    for pl in resp.json().get("playlists", []):
        if pl.get("name") == ABS_PICKS_PLAYLIST_NAME and pl.get("description") == ABS_PICKS_DESCRIPTION:
            return pl["id"]
    return None


def sync_picks_playlist(profile_id: int, abs_url: str, abs_token: str) -> int:
    """
    Sync the 'Bookclub Picks' ABS playlist for a profile, in confidence order.
    Reuses the cached playlist, else one found by name and description, else creates it.
    No picks leaves the playlist alone (emptying it would make ABS delete it).
    Returns item count, 0 on failure or when there are no picks.
    """
    picks = db.get_bookclub_picks(profile_id)
    ids = _abs_filter_existing(list(dict.fromkeys(row["abs_library_item_id"] for row in picks)))
    if not ids:
        db.log("abs", f"Bookclub Picks: no picks, leaving the ABS playlist as is (profile {profile_id})")
        return 0

    profile = db.get_profile(profile_id)
    pl_id = profile["abs_picks_playlist_id"] if profile else None
    headers = {"Authorization": f"Bearer {abs_token}"}

    try:
        status = None
        if pl_id:
            status = _abs_set_playlist_items(abs_url, abs_token, pl_id, ids, allow_empty=False)
        if status in (None, "missing"):
            with _abs_http() as client:
                pl_id = _find_picks_playlist(client, abs_url, headers)
            status = (_abs_set_playlist_items(abs_url, abs_token, pl_id, ids, allow_empty=False)
                      if pl_id else "missing")

        if status == "missing":
            db.update_profile_picks_playlist_id(profile_id, None)
            library_id = _abs_library_id_for_item(ids[0])
            if not library_id:
                db.log("abs", f"Bookclub Picks: library id not found (profile {profile_id})",
                       level="warning")
                return 0
            with _abs_http() as client:
                resp = client.post(
                    f"{abs_url}/api/playlists",
                    headers=headers,
                    json={
                        "name": ABS_PICKS_PLAYLIST_NAME,
                        "libraryId": library_id,
                        "description": ABS_PICKS_DESCRIPTION,
                        "items": [{"libraryItemId": i, "episodeId": None} for i in ids],
                    },
                )
                resp.raise_for_status()
                db.update_profile_picks_playlist_id(profile_id, resp.json()["id"])
            status = "created"
        else:
            db.update_profile_picks_playlist_id(profile_id, pl_id)
        db.log("abs", f"Bookclub Picks {status}: {len(ids)} items (profile {profile_id})")
        return len(ids)
    except Exception as e:
        db.log("abs", f"Failed to sync Bookclub Picks (profile {profile_id}): {e}", level="error")
        return 0


# ---------------------------------------------------------------------------
# Full sync orchestrator (called from the web route)
# ---------------------------------------------------------------------------

def run_full_sync(profile_id: int = 1) -> dict:
    log_id = db.start_sync_log()
    db.log("sync", "Sync started")
    try:
        hc_count = sync_hardcover()
        db.log("sync", f"Hardcover sync complete — {hc_count} books")

        with db.db() as conn:
            recs = conn.execute(
                "SELECT id, title, hc_book_id, hardcover_id, abs_library_item_id "
                "FROM recommendations"
            ).fetchall()

        abs_count = sync_abs(recs)
        db.log("sync", f"ABS library sync complete — {abs_count} recommendations cross-referenced")

        link_recs_to_hc(recs)

        playlist_count = sync_abs_playlist(profile_id)
        if playlist_count:
            db.log("abs", f"ABS playlist synced — queue has {playlist_count} items")

        # Sync Bookclub Picks playlist for every profile with an ABS token
        picks_url = ABS_URL
        for p in db.get_profiles():
            token = p["abs_token"] or (ABS_TOKEN if p["id"] == profile_id else None)
            if not token or not picks_url:
                continue
            picks_count = sync_picks_playlist(p["id"], picks_url, token)
            if picks_count:
                db.log("abs", f"Bookclub Picks: {picks_count} items for '{p['name']}'")

        db.finish_sync_log(log_id, hc_count, abs_count, "ok")
        db.log("sync", "Sync finished OK")
        return {"status": "ok", "hc_synced": hc_count, "abs_synced": abs_count,
                "playlist_synced": playlist_count}
    except Exception as e:
        db.finish_sync_log(log_id, 0, 0, "error", str(e))
        db.log("sync", f"Sync failed: {e}", level="error", detail=str(e))
        return {"status": "error", "message": str(e)}
