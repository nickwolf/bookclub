"""
Sync logic:
  1. Pull all user books from Hardcover (paginated)
  2. Read ABS SQLite DB for library items + listen history
  3. Cross-reference recommendations against both sources
  4. Seed recommendations from the hardcoded list if DB is empty
"""

import json
import os
import re
import difflib
import sqlite3
import unicodedata
import httpx

import db

HARDCOVER_API = "https://api.hardcover.app/v1/graphql"
ABS_DB_PATH          = os.environ.get("ABS_DB_PATH", "/abs_config/absdatabase.sqlite")
ABS_URL              = os.environ.get("ABS_URL", "")
ABS_TOKEN            = os.environ.get("ABS_TOKEN", "")
ABS_PLAYLIST_ID      = os.environ.get("ABS_PLAYLIST_ID", "")
HARDCOVER_TOKEN      = os.environ.get("HARDCOVER_TOKEN", "")
ABS_PICKS_PLAYLIST_NAME = "Bookclub Picks"

HC_QUERY = """
query GetUserBooks($limit: Int!, $offset: Int!) {
  me {
    user_books(limit: $limit, offset: $offset) {
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

def _norm(title: str) -> str:
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    title = title.lower()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\b(the|a|an)\b", "", title)
    return re.sub(r"\s+", " ", title).strip()


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


def sync_hardcover() -> int:
    """Pull all user books from Hardcover and upsert into hc_books. Returns count synced."""
    total = 0
    offset = 0
    limit = 100

    with httpx.Client(timeout=30) as client:
        while True:
            resp = client.post(
                HARDCOVER_API,
                headers=_hc_headers(),
                json={"query": HC_QUERY, "variables": {"limit": limit, "offset": offset}},
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"Hardcover API error: {data['errors']}")
            me = data.get("data", {}).get("me", [])
            if not me:
                raise RuntimeError("Hardcover API returned no user data — check token")
            user_books = me[0].get("user_books", [])
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
                total += 1

            offset += limit
            if len(user_books) < limit:
                break

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


def push_queue_to_abs(profile_id: int = 1) -> bool:
    """
    Push the current Bookclub queue order to the ABS Reading List playlist.
    Only includes items that have an ABS library item ID.
    Returns True on success.
    """
    if not ABS_URL or not ABS_TOKEN or not ABS_PLAYLIST_ID:
        return False

    queue_items = db.get_queue_abs_items(profile_id)
    items = [{"libraryItemId": row["abs_library_item_id"], "episodeId": None}
             for row in queue_items]

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.patch(
                f"{ABS_URL}/api/playlists/{ABS_PLAYLIST_ID}",
                headers={"Authorization": f"Bearer {ABS_TOKEN}"},
                json={"items": items},
            )
            resp.raise_for_status()
        db.log("abs", f"Pushed queue to ABS playlist — {len(items)} items")
        return True
    except Exception as e:
        db.log("abs", f"Failed to push queue to ABS: {e}", level="error")
        return False


# ---------------------------------------------------------------------------
# Link recommendations ↔ Hardcover books
# ---------------------------------------------------------------------------

def link_recs_to_hc(rec_rows: list):
    """Try to match each recommendation to a Hardcover book by title fuzzy match."""
    with db.db() as conn:
        hc_books = conn.execute(
            "SELECT id, lower(title) as norm_title FROM hc_books"
        ).fetchall()

    hc_norm_map = {row["norm_title"]: row["id"] for row in hc_books}
    hc_norms = list(hc_norm_map.keys())

    for rec in rec_rows:
        if rec["hc_book_id"]:
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

def _get_abs_library_id(abs_url: str, abs_token: str) -> str | None:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{abs_url}/api/libraries",
                              headers={"Authorization": f"Bearer {abs_token}"})
            resp.raise_for_status()
            libraries = resp.json().get("libraries", [])
            return libraries[0]["id"] if libraries else None
    except Exception as e:
        db.log("abs", f"Could not get ABS library ID: {e}", level="warning")
        return None



def sync_picks_playlist(profile_id: int, abs_url: str, abs_token: str) -> int:
    """
    Rebuild the 'Bookclub Picks' ABS playlist for a profile.
    Deletes the old playlist and creates a fresh one with items in confidence order.
    (ABS PATCH is broken for playlist items — POST on creation is the reliable path.)
    Returns item count, 0 on failure.
    """
    picks = db.get_bookclub_picks(profile_id)

    library_id = _get_abs_library_id(abs_url, abs_token)
    if not library_id:
        return 0

    # Delete existing playlist if we have one cached
    profile = db.get_profile(profile_id)
    old_pl_id = profile["abs_picks_playlist_id"] if profile else None
    if old_pl_id:
        try:
            with httpx.Client(timeout=10) as client:
                client.delete(f"{abs_url}/api/playlists/{old_pl_id}",
                              headers={"Authorization": f"Bearer {abs_token}"})
        except Exception:
            pass
        db.update_profile_picks_playlist_id(profile_id, None)

    if not picks:
        return 0

    items = [{"libraryItemId": row["abs_library_item_id"], "episodeId": None}
             for row in picks]
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{abs_url}/api/playlists",
                headers={"Authorization": f"Bearer {abs_token}"},
                json={
                    "name": ABS_PICKS_PLAYLIST_NAME,
                    "libraryId": library_id,
                    "description": "AI-curated recommendations already in your library, ordered by match confidence.",
                    "items": items,
                },
            )
            resp.raise_for_status()
            pl_id = resp.json()["id"]
            db.update_profile_picks_playlist_id(profile_id, pl_id)
        db.log("abs", f"Bookclub Picks rebuilt — {len(items)} items (profile {profile_id})")
        return len(items)
    except Exception as e:
        db.log("abs", f"Failed to rebuild Bookclub Picks (profile {profile_id}): {e}", level="error")
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
                "SELECT id, title, hc_book_id, abs_library_item_id FROM recommendations"
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
