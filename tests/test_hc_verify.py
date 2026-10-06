import json

import httpx
import pytest

import gen
import sync
from test_gen import FakeClient, _msg, _rec, _text  # noqa: F401  (fixtures reused below)


def _doc(id_, title, authors, users=10, image=True, featured=None):
    return {"document": {
        "id": str(id_), "title": title, "author_names": authors, "users_count": users,
        "image": {"url": f"https://assets.hardcover.app/{id_}.jpg"} if image else None,
        "featured_series": featured,
    }}


def _series(name, pos):
    return {"position": pos, "series": {"name": name}}


DECOYS = [
    _doc(1, "City of Glass: The Graphic Novel", ["Paul Auster", "Paul Karasik"], users=900),
    _doc(2, "The Glass of Time", ["Michael Cox"], users=300),
]
WILL = _doc(594985, "The Will of the Many", ["James Islington"], users=5856,
            featured=_series("Hierarchy", 1.0))


def _search_client(responses, seen=None):
    """MockTransport client; each response is a list of hit docs, an int status, or an Exception."""
    it = iter(responses)

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            return httpx.Response(r, json={})
        if isinstance(r, dict):
            return httpx.Response(200, json=r)
        return httpx.Response(200, json={"data": {"search": {"results": {
            "found": len(r), "hits": r}}}})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def hc(monkeypatch, test_db):
    monkeypatch.setattr(sync, "_sleep", lambda s: None)
    monkeypatch.setattr(sync, "HARDCOVER_TOKEN", "tok")
    return test_db


def test_picks_matching_hit_over_decoys(hc):
    seen = []
    c = _search_client([DECOYS + [WILL]], seen)
    m = sync.search_hc_book(c, "The Will of the Many", "James Islington")
    assert m == {"hardcover_id": 594985, "title": "The Will of the Many",
                 "author": "James Islington",
                 "cover_url": "https://assets.hardcover.app/594985.jpg",
                 "series": "Hierarchy", "series_pos": 1.0, "prefix_only": False}
    assert seen[0]["variables"]["q"] == "The Will of the Many James Islington"
    assert seen[0]["variables"]["n"] == 5


def test_series_prefix_title_matches_most_read_volume(hc):
    hits = [_doc(10, "Mistborn: Secret History", ["Brandon Sanderson"], users=3000),
            _doc(11, "Mistborn: The Final Empire", ["Brandon Sanderson"], users=90000),
            _doc(12, "Mistborn: The Final Empire", ["Somebody Else"], users=99999)]
    m = sync.search_hc_book(_search_client([hits]), "Mistborn", "Brandon Sanderson")
    assert m["hardcover_id"] == 11


def test_series_prefix_needs_author(hc):
    hits = [_doc(11, "Mistborn: The Final Empire", ["Brandon Sanderson"], users=90000)]
    assert sync.search_hc_book(_search_client([hits]), "Mistborn", "") is None


def test_exact_title_beats_series_prefix(hc):
    hits = [_doc(20, "Dune: House Atreides", ["Brian Herbert", "Kevin J. Anderson"], users=50000),
            _doc(21, "Dune", ["Frank Herbert"], users=40000)]
    assert sync.search_hc_book(_search_client([hits]), "Dune", "Frank Herbert")["hardcover_id"] == 21


def test_made_up_title_matches_nothing(hc):
    c = _search_client([DECOYS])
    assert sync.search_hc_book(c, "The Glass Cartographer of Velmora", "Penelope Starling") is None


def test_author_must_agree(hc):
    c = _search_client([[_doc(5, "The Will of the Many", ["Someone Else"])]])
    assert sync.search_hc_book(c, "The Will of the Many", "James Islington") is None


def test_fuzzy_title_with_author(hc):
    c = _search_client([[_doc(6, "Mistborn: The Final Empires", ["Brandon Sanderson"])]])
    assert sync.search_hc_book(c, "Mistborn: The Final Empire", "Brandon Sanderson")["hardcover_id"] == 6


def test_placeholder_author_needs_exact_title_key(hc):
    hits = [_doc(7, "The Will of the Many", ["James Islington"]),
            _doc(8, "The Will of the Manor", ["Other Person"], users=99999)]
    m = sync.search_hc_book(_search_client([hits]), "The Will of the Many", "Unknown")
    assert m["hardcover_id"] == 7
    assert sync.search_hc_book(_search_client([[hits[1]]]), "The Will of the Many", "") is None


def test_highest_users_count_wins_and_string_id_is_int(hc):
    hits = [_doc(11, "The Will of the Many", ["James Islington"], users=5),
            _doc(12, "The Will of the Many", ["James Islington"], users=500)]
    m = sync.search_hc_book(_search_client([hits]), "The Will of the Many", "James Islington")
    assert m["hardcover_id"] == 12 and isinstance(m["hardcover_id"], int)


def test_null_image_and_featured_series(hc):
    c = _search_client([[_doc(13, "Standalone", ["Ann Author"], image=False)]])
    m = sync.search_hc_book(c, "Standalone", "Ann Author")
    assert m["cover_url"] is None and m["series"] is None and m["series_pos"] is None


def test_graphql_errors_raise(hc):
    c = _search_client([{"errors": [{"message": "boom"}]}])
    with pytest.raises(RuntimeError):
        sync.search_hc_book(c, "T", "A")


def test_http_error_raises(hc):
    with pytest.raises(httpx.HTTPStatusError):
        sync.search_hc_book(_search_client([500]), "T", "A")


# generation

@pytest.fixture
def run(monkeypatch, test_db):
    """Run generation with the model returning `recs` and Hardcover answering `responses`."""
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(gen, "_fetch_covers_sync", lambda recs: covers.extend(recs))
    monkeypatch.setattr(sync, "_sleep", lambda s: None)
    monkeypatch.setattr(sync, "HARDCOVER_TOKEN", "tok")
    covers = []

    def go(recs, responses):
        monkeypatch.setattr(gen, "_client", lambda: FakeClient(_msg([_text(recs)])))
        real = httpx.Client
        client = _search_client(responses, getattr(go, "seen", None))
        monkeypatch.setattr(gen.httpx, "Client", lambda **kw: client)
        try:
            return gen.run_generation(1, len(recs))
        finally:
            gen.httpx.Client = real
    go.covers = covers
    return go


def _rows(db):
    with db.db() as conn:
        return {r["title"]: r for r in conn.execute("SELECT * FROM recommendations")}


def _logs(db):
    with db.db() as conn:
        return [r["message"] for r in conn.execute("SELECT * FROM app_log")]


def test_generation_enriches_matched_and_drops_unmatched(run, test_db):
    recs = [_rec("Will of the Many", series="", author="James Islington"),
            _rec("The Glass Cartographer of Velmora", author="Penelope Starling")]
    assert run(recs, [[WILL], DECOYS, DECOYS]) == {"added": 1, "unverified": 0}
    rows = _rows(test_db)
    assert set(rows) == {"The Will of the Many"}
    r = rows["The Will of the Many"]
    assert (r["hardcover_id"], r["series"], r["series_pos"]) == (594985, "Hierarchy", 1.0)
    assert r["cover_url"].endswith("594985.jpg")
    assert run.covers == []
    assert any("Dropped, not found on Hardcover: The Glass Cartographer" in m for m in _logs(test_db))


def test_match_keeps_model_author_over_contributor_list(run, test_db):
    dcc = _doc(446681, "Dungeon Crawler Carl", ["Matt Dinniman", "Will Staehle"], users=9000)
    assert run([_rec("Dungeon Crawler Carl", author="Matt Dinniman")], [[dcc]]) == {"added": 1, "unverified": 0}
    assert _rows(test_db)["Dungeon Crawler Carl"]["author"] == "Matt Dinniman"


def test_circuit_breaker_keeps_unverified_and_stops(run, test_db):
    recs = [_rec("One", author="A"), _rec("Two", author="A"), _rec("Three", author="A")]
    assert run(recs, [500]) == {"added": 3, "unverified": 3}
    rows = _rows(test_db)
    assert all(r["hardcover_id"] is None for r in rows.values())
    assert len(run.covers) == 3
    warns = [m for m in _logs(test_db) if "Hardcover verification failed" in m]
    assert len(warns) == 1


def test_no_token_keeps_unverified(run, test_db, monkeypatch):
    monkeypatch.setattr(sync, "HARDCOVER_TOKEN", "")
    assert run([_rec("One")], []) == {"added": 1, "unverified": 1}
    assert _rows(test_db)["One"]["hardcover_id"] is None


def test_id_dedup_against_shelf_catalog_and_batch(run, test_db):
    test_db.upsert_hc_book(100, "Shelf Book", "Zed Different", None, None, None, 3, None)
    test_db.upsert_recommendation("Cat Book", "Zed Different", None, "Book", "Yes", "r",
                                  hardcover_id=200)
    recs = [_rec("Shelf Book", author="Sam Shelf"), _rec("Cat Book", author="Cat Author"),
            _rec("Fresh One", author="New Author"), _rec("Fresh One!", author="New Author")]
    resp = [[_doc(100, "Shelf Book", ["Sam Shelf"])],
            [_doc(200, "Cat Book", ["Cat Author"])],
            [_doc(300, "Fresh One", ["New Author"])],
            [_doc(300, "Fresh One", ["New Author"])]]
    assert run(recs, resp) == {"added": 1, "unverified": 0}
    assert "Fresh One" in _rows(test_db)
    assert set(_rows(test_db)) == {"Cat Book", "Fresh One"}


def test_upsert_stores_and_fills_new_columns(test_db):
    rid = test_db.upsert_recommendation("T", "A", None, "Book", "Yes", "r",
                                        hardcover_id=5, series_pos=2.5, cover_url="u")
    row = _rows(test_db)["T"]
    assert (row["hardcover_id"], row["series_pos"], row["cover_url"]) == (5, 2.5, "u")
    test_db.upsert_recommendation("T", "A", None, "Book", "Yes", "r",
                                  hardcover_id=9, series_pos=1.0, cover_url="v")
    row = _rows(test_db)["T"]
    assert (row["hardcover_id"], row["series_pos"], row["cover_url"]) == (5, 2.5, "u")
    rid2 = test_db.upsert_recommendation("U", "A", None, "Book", "Yes", "r")
    test_db.upsert_recommendation("U", "A", None, "Book", "Yes", "r", hardcover_id=7)
    assert _rows(test_db)["U"]["hardcover_id"] == 7 and rid != rid2


def test_link_recs_to_hc_exact_by_id(test_db):
    test_db.upsert_hc_book(50, "Totally Different Name", "A", None, None, None, 3, None)
    rid = test_db.upsert_recommendation("Unrelated Title", "A", None, "Book", "Yes", "r",
                                        hardcover_id=50)
    with test_db.db() as conn:
        rows = conn.execute(
            "SELECT id, title, hc_book_id, hardcover_id FROM recommendations").fetchall()
    sync.link_recs_to_hc(rows)
    with test_db.db() as conn:
        assert conn.execute("SELECT hc_book_id FROM recommendations WHERE id = ?",
                            (rid,)).fetchone()[0] == 50


def _sdoc(id_, name, author, readers=1):
    return {"document": {"id": str(id_), "name": name, "author_name": author,
                         "readers_count": readers}}


STORM = [
    _sdoc(257571, "Cosmere Roleplaying Game", "Lydia Suen", 6),
    _sdoc(997, "The Stormlight Archive", "Brandon Sanderson", 25043),
    _sdoc(282411, "The Stormlight Archive (Split Volume Edition)", "Brandon Sanderson", 946),
]


def _series_rec(title="The Stormlight Archive", author="Brandon Sanderson"):
    return _rec(title, author=author, type="Series")


def test_series_search_picks_most_read_and_rejects_decoy(hc):
    seen = []
    m = sync.search_hc_series(_search_client([STORM], seen), "The Stormlight Archive",
                              "Brandon Sanderson")
    assert m == {"title": "The Stormlight Archive", "author": "Brandon Sanderson"}
    assert seen[0]["variables"]["t"] == "Series"
    only_decoy = _search_client([[STORM[0]]])
    assert sync.search_hc_series(only_decoy, "The Stormlight Archive", "Brandon Sanderson") is None


def test_series_rec_falls_back_to_series_search(run, test_db):
    rec = _series_rec("Stormlight Archive")
    assert run([rec], [DECOYS, DECOYS, STORM]) == {"added": 1, "unverified": 0}
    r = _rows(test_db)["The Stormlight Archive"]
    assert (r["series"], r["hardcover_id"], r["series_pos"], r["cover_url"]) == (
        "The Stormlight Archive", None, None, None)
    assert len(run.covers) == 1


def test_unmatched_series_rec_dropped(run, test_db):
    assert run([_series_rec("Velmora Cycle", "Penelope Starling")],
               [DECOYS, DECOYS, [STORM[0]]]) == {"added": 0, "unverified": 0}


def test_book_rec_does_not_trigger_series_search(run, test_db):
    seen = []
    run.seen = seen
    assert run([_rec("Velmora", author="Penelope Starling")], [DECOYS, DECOYS]) == {"added": 0, "unverified": 0}
    assert [q["variables"]["t"] for q in seen] == ["Book", "Book"]


def test_series_search_error_trips_breaker(run, test_db):
    recs = [_series_rec(), _rec("Two", author="A")]
    assert run(recs, [DECOYS, DECOYS, 500]) == {"added": 2, "unverified": 2}
    assert all(r["hardcover_id"] is None for r in _rows(test_db).values())
    assert len([m for m in _logs(test_db) if "verification failed" in m]) == 1


def test_num_filter():
    import main
    f = main.templates.env.filters["num"]
    assert f(1.0) == "1" and f(2.5) == "2.5"


FOUNDATION = [
    _doc(1, "Foundation: Book Two", ["Isaac Asimov"], users=9000),
    _doc(2, "Foundation: Book One", ["Isaac Asimov"], users=8000),
    _doc(3, "Foundation", ["Isaac Asimov"], users=100),
]


def test_exact_title_beats_popular_sibling_volume(hc):
    m = sync.search_hc_book(_search_client([FOUNDATION]), "Foundation", "Isaac Asimov")
    assert m["hardcover_id"] == 3


def test_series_hit_ranked_by_title_closeness_first(hc):
    docs = [_sdoc(1, "The Stormlight Archive (Split Volume Edition)", "Brandon Sanderson", 99999),
            _sdoc(2, "The Stormlight Archive", "Brandon Sanderson", 5)]
    m = sync.search_hc_series(_search_client([docs]), "The Stormlight Archive", "Brandon Sanderson")
    assert m["title"] == "The Stormlight Archive"


def test_title_only_retry_rescues_a_miss(run, test_db):
    seen = []
    run.seen = seen
    rec = _rec("The Will of the Many", author="James Islington")
    assert run([rec], [DECOYS, [WILL]]) == {"added": 1, "unverified": 0}
    assert seen[0]["variables"]["q"] == "The Will of the Many James Islington"
    assert seen[1]["variables"]["q"] == "The Will of the Many"
    assert seen[1]["variables"]["n"] == 10
    assert _rows(test_db)["The Will of the Many"]["hardcover_id"] == 594985


def test_budget_trips_breaker_and_counts_unverified(run, test_db, monkeypatch):
    ticks = iter([0, 0, 500, 500, 500])
    monkeypatch.setattr(gen, "_monotonic", lambda: next(ticks))
    recs = [_rec("Will of the Many", author="James Islington"),
            _rec("Two", author="A"), _rec("Three", author="A")]
    assert run(recs, [[WILL]]) == {"added": 3, "unverified": 2}
    assert any("ran out of time" in m for m in _logs(test_db))


def test_verification_limits_rate_limit_waits(hc, monkeypatch):
    slept = []
    monkeypatch.setattr(sync, "_sleep", slept.append)
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"retry-after": "60"}, json={})))
    with pytest.raises(httpx.HTTPStatusError):
        sync.search_hc_book(c, "T", "A", attempts=3, max_wait=20)
    assert slept == [20, 20]


def test_cover_precedence(test_db):
    test_db.upsert_hc_book(1, "Vol 1", "A", "S", 1, "shelf.jpg", 3, None)
    verified = test_db.upsert_recommendation("Vol 2", "A", "S", "Book", "Yes", "r",
                                             hardcover_id=9, cover_url="verified.jpg")
    unverified = test_db.upsert_recommendation("Vol 3", "A", "S", "Book", "Yes", "r",
                                               cover_url="ol.jpg")
    assert test_db.get_rec_detail(verified)["cover_url"] == "verified.jpg"
    assert test_db.get_rec_detail(unverified)["cover_url"] == "shelf.jpg"


def test_known_hardcover_id_off_shelf_is_not_fuzzy_linked(test_db):
    test_db.upsert_hc_book(50, "Same Title", "A", None, None, None, 3, None)
    rid = test_db.upsert_recommendation("Same Title", "A", None, "Book", "Yes", "r",
                                        hardcover_id=999)
    with test_db.db() as conn:
        rows = conn.execute(
            "SELECT id, title, hc_book_id, hardcover_id FROM recommendations").fetchall()
    sync.link_recs_to_hc(rows)
    with test_db.db() as conn:
        assert conn.execute("SELECT hc_book_id FROM recommendations WHERE id = ?",
                            (rid,)).fetchone()[0] is None


def test_card_series_position_precedence(client, test_db):
    rid = test_db.upsert_recommendation("T", "A", "S", "Book", "Yes", "r", series_pos=2.0)
    assert " #2" in client.get(f"/rec/{rid}/detail").text
    with test_db.db() as conn:
        conn.execute("UPDATE recommendations SET abs_series_seq = '3' WHERE id = ?", (rid,))
    row = test_db.get_rec_detail(rid)
    assert row["abs_series_seq"] == "3" and row["series_pos"] == 2.0


def test_status_shows_unverified_note(client):
    import main
    main._gen_last = {"added": 4, "unverified": 3, "error": None, "finished_at": None}
    assert "3 not verified, Hardcover was unavailable." in client.get("/recs/generate/status", headers={"HX-Request": "true"}).text
    main._gen_last = None


def test_series_prefix_prefers_first_volume_over_popularity(hc):
    hits = [_doc(10, "Mistborn: Secret History", ["Brandon Sanderson"], users=99000),
            _doc(11, "Mistborn: The Final Empire", ["Brandon Sanderson"], users=90000,
                 featured=_series("Mistborn", 1.0))]
    m = sync.search_hc_book(_search_client([hits]), "Mistborn", "Brandon Sanderson")
    assert (m["hardcover_id"], m["prefix_only"]) == (11, True)


def test_prefix_only_match_is_provisional(run, test_db):
    prefix = [_doc(10, "Mistborn: Secret History", ["Brandon Sanderson"], users=3000)]
    exact = [_doc(30, "Mistborn", ["Brandon Sanderson"], users=500)]
    assert run([_rec("Mistborn", author="Brandon Sanderson")], [prefix, exact])["added"] == 1
    assert _rows(test_db)["Mistborn"]["hardcover_id"] == 30


def test_prefix_only_kept_when_wider_search_finds_nothing_better(run, test_db):
    prefix = [_doc(10, "Mistborn: The Final Empire", ["Brandon Sanderson"], users=3000)]
    assert run([_rec("Mistborn", author="Brandon Sanderson")], [prefix, DECOYS])["added"] == 1
    assert _rows(test_db)["Mistborn: The Final Empire"]["hardcover_id"] == 10


def test_budget_checked_before_every_search(run, test_db, monkeypatch):
    ticks = iter([0, 0, 500])
    monkeypatch.setattr(gen, "_monotonic", lambda: next(ticks))
    seen = []
    run.seen = seen
    rec = _rec("Nothing Here", author="Nobody")
    assert run([rec], [DECOYS]) == {"added": 1, "unverified": 1}
    assert len(seen) == 1
