from textnorm import filter_duplicates, title_keys


def _blocked(*titles):
    keys = set()
    for t in titles:
        keys |= title_keys(t)
    return keys


def _dups(candidate, *existing, **kw):
    kept, _ = filter_duplicates([{"title": candidate, **kw}], _blocked(*existing))
    return not kept


def test_subtitle_half_matches():
    assert _dups("Mistborn: The Final Empire", "The Final Empire")


def test_trailing_subtitle_blocked():
    assert _dups("Project Hail Mary: A Novel", "Project Hail Mary")
    assert _dups("Project Hail Mary", "Project Hail Mary: A Novel")


def test_parenthetical_stripped():
    assert _dups("Leviathan Wakes (The Expanse, #1)", "Leviathan Wakes")


def test_sequel_not_blocked():
    assert not _dups("Dune Messiah", "Dune")
    assert not _dups("Dune", "Dune Messiah")


def test_generic_part_is_not_a_key():
    assert "book one" not in title_keys("Mistborn: Book One")
    assert not _dups("Book One", "Mistborn: Book One")


def test_batch_internal_dedup():
    kept, skipped = filter_duplicates(
        [{"title": "Dune"}, {"title": "dune"}, {"title": "Hyperion"}], set())
    assert [r["title"] for r in kept] == ["Dune", "Hyperion"]
    assert len(skipped) == 1


def test_series_blocked_by_started_series():
    kept, _ = filter_duplicates(
        [{"title": "The Expanse", "type": "Series", "series": "The Expanse"}],
        set(), {"expanse"})
    assert not kept


def test_db_blocked_keys(test_db):
    test_db.upsert_hc_book(1, "Leviathan Wakes", "A", "The Expanse", 1, None, test_db.HC_READ, None)
    test_db.upsert_hc_book(2, "Want Me", "A", None, None, None, test_db.HC_WANT_TO_READ, None)
    keys, series = test_db.get_blocked_title_keys()
    assert {"leviathan wakes", "want me"} <= keys
    assert series == {"expanse"}
