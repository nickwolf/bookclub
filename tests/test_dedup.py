from textnorm import add_blocked, filter_duplicates, title_keys


def _blocked(*titles, author=None):
    keys = {}
    for t in titles:
        add_blocked(keys, t, author)
    return keys


def _dups(candidate, *existing, existing_author=None, **kw):
    kept, _ = filter_duplicates([{"title": candidate, **kw}], _blocked(*existing, author=existing_author))
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
        [{"title": "Dune"}, {"title": "dune"}, {"title": "Hyperion"}], {})
    assert [r["title"] for r in kept] == ["Dune", "Hyperion"]
    assert len(skipped) == 1


def test_series_blocked_by_started_series():
    kept, _ = filter_duplicates(
        [{"title": "The Expanse", "type": "Series", "series": "The Expanse"}],
        {}, {"expanse"})
    assert not kept


def test_db_blocked_keys(test_db):
    test_db.upsert_hc_book(1, "Leviathan Wakes", "A", "The Expanse", 1, None, test_db.HC_READ, None)
    test_db.upsert_hc_book(2, "Want Me", "A", None, None, None, test_db.HC_WANT_TO_READ, None)
    keys, series = test_db.get_blocked_title_keys()
    assert {"leviathan wakes", "want me"} <= keys.keys()
    assert keys["leviathan wakes"] == {"a"}
    assert series == {"expanse"}


def test_generic_subtitle_blocked_by_base():
    assert _dups("Wool: A Novel", "Wool")
    assert _dups("Dune: Part Two", "Dune")
    assert not _dups("Dune Messiah", "Dune")


def test_started_series_blocks_books():
    kept, _ = filter_duplicates(
        [{"title": "Caliban's War", "type": "Book", "series": "The Expanse"}], {}, {"expanse"})
    assert not kept


def test_fuzzy_needs_author_agreement():
    assert not _dups("The Martians", "The Martian", existing_author="Andy Weir",
                     author="Kim Stanley Robinson")
    assert _dups("The Martiann", "The Martian", existing_author="Andy Weir", author="Andy Weir")
    # unknown author on either side falls back to title-only
    assert _dups("The Martians", "The Martian", existing_author="Andy Weir")
    assert _dups("The Martians", "The Martian", author="Andy Weir")


def test_exact_key_blocks_regardless_of_author():
    assert _dups("The Martian", "The Martian", existing_author="Andy Weir", author="Someone Else")


def test_author_list_overlap():
    assert _dups("The Martiann", "The Martian", existing_author="Andy Weir, Other Person",
                 author="Person Other, Andy Weir")


def test_placeholder_author_is_unknown():
    for placeholder in ("Unknown", "unknown author", "ANONYMOUS", "N/A", "Various", "", "  "):
        assert _dups("The Martiann", "The Martian", existing_author="Andy Weir",
                     author=placeholder), placeholder
