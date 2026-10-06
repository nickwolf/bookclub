import main
import gen


def _add(db, bid, title, status, rating=None):
    db.upsert_hc_book(bid, title, "Author", None, None, None, status, rating)


def test_status_labels_cover_all_six():
    assert main.HC_STATUS == {
        1: "Want to Read", 2: "Reading", 3: "Read",
        4: "Paused", 5: "DNF", 6: "Ignored",
    }


def test_rec_context_maps_statuses(test_db):
    _add(test_db, 1, "Paused Book", test_db.HC_PAUSED)
    _add(test_db, 2, "Dropped Book", test_db.HC_DNF)
    _add(test_db, 3, "Ignored Book", test_db.HC_IGNORED)
    ctx = test_db.get_rec_context()
    assert [b["title"] for b in ctx["paused_books"]] == ["Paused Book"]
    assert [b["title"] for b in ctx["dnf_books"]] == ["Dropped Book"]
    prompt = gen.build_prompt(ctx, 5)
    assert "paused" in prompt and "Paused Book" in prompt


def test_local_rating_survives_sync_without_rating(test_db):
    _add(test_db, 1, "Book", test_db.HC_READ, rating=None)
    with test_db.db() as conn:
        conn.execute("UPDATE hc_books SET rating = 4 WHERE id = 1")
    _add(test_db, 1, "Book", test_db.HC_READ, rating=None)
    with test_db.db() as conn:
        assert conn.execute("SELECT rating FROM hc_books WHERE id = 1").fetchone()[0] == 4


def test_hc_rating_overwrites(test_db):
    _add(test_db, 1, "Book", test_db.HC_READ, rating=4)
    _add(test_db, 1, "Book", test_db.HC_READ, rating=2)
    with test_db.db() as conn:
        assert conn.execute("SELECT rating FROM hc_books WHERE id = 1").fetchone()[0] == 2


def test_full_history_in_prompt_without_overlap(test_db):
    n = 0
    for status, rating in [(test_db.HC_READ, 5), (test_db.HC_READ, 1), (test_db.HC_READ, 3),
                           (test_db.HC_READ, None), (test_db.HC_WANT_TO_READ, None),
                           (test_db.HC_DNF, None)]:
        for i in range(70):
            n += 1
            _add(test_db, n, f"Title{n:04d}", status, rating)
    prompt = gen.build_prompt(test_db.get_rec_context(), 5)
    for i in range(1, n + 1):
        assert prompt.count(f"Title{i:04d} by") == 1


def test_zero_rating_does_not_overwrite(test_db):
    _add(test_db, 1, "Book", test_db.HC_READ, rating=4)
    _add(test_db, 1, "Book", test_db.HC_READ, rating=0)
    with test_db.db() as conn:
        assert conn.execute("SELECT rating FROM hc_books WHERE id = 1").fetchone()[0] == 4
