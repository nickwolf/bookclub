import json

import httpx
import pytest

import sync

URL = "http://abs.test"


class FakeAbs:
    """Stateful stand-in for the ABS playlist endpoints; records every call."""

    def __init__(self, playlists=None, known=None):
        self.playlists = playlists or {}  # id -> {"name": str, "items": [ids], "description": str}
        self.known = known  # library item ids ABS knows; None accepts everything
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path))
        if request.method == "GET" and path == "/api/playlists":
            return httpx.Response(200, json={"playlists": [
                {"id": i, "name": p["name"], "description": p.get("description"), "items": []} for i, p in self.playlists.items()]})
        if request.method == "POST" and path == "/api/playlists":
            pid = "new-id"
            if self.known is not None and any(i["libraryItemId"] not in self.known for i in body["items"]):
                return httpx.Response(400, text="Invalid request body items")
            self.playlists[pid] = {"name": body["name"],
                                   "items": [i["libraryItemId"] for i in body["items"]]}
            return httpx.Response(200, json={"id": pid})
        pid = path.split("/")[3]
        pl = self.playlists.get(pid)
        if pl is None:
            return httpx.Response(404, text="Playlist not found")
        if request.method == "GET":
            return httpx.Response(200, json={"id": pid, "name": pl["name"], "items": [
                {"libraryItemId": i, "libraryItem": {}} for i in pl["items"]]})
        ids = [i["libraryItemId"] for i in body["items"]]
        if path.endswith("/batch/add"):
            if self.known is not None and any(i not in self.known for i in ids):
                return httpx.Response(400, text="Invalid request body items")
            pl["items"] += [i for i in ids if i not in pl["items"]]
        elif path.endswith("/batch/remove"):
            pl["items"] = [i for i in pl["items"] if i not in ids]
            if not pl["items"]:
                del self.playlists[pid]
        elif request.method == "PATCH":
            if sorted(ids) != sorted(pl["items"]):
                return httpx.Response(400, text="Invalid playlist items. Length mismatch")
            pl["items"] = ids
        return httpx.Response(200, json={})

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture()
def abs_server(monkeypatch):
    def install(playlists=None, known=None):
        fake = FakeAbs(playlists, known)
        monkeypatch.setattr(sync, "_abs_http",
                            lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
        return fake
    return install


def _set(ids, allow_empty=False, pid="p1"):
    return sync._abs_set_playlist_items(URL, "tok", pid, ids, allow_empty)


def test_reorder_only_sends_patch(abs_server):
    fake = abs_server({"p1": {"name": "RL", "items": ["a", "b", "c"]}})
    assert _set(["c", "a", "b"]) == "updated"
    assert fake.writes() == [("PATCH", "/api/playlists/p1")]
    assert fake.playlists["p1"]["items"] == ["c", "a", "b"]


def test_unchanged_sends_no_writes(abs_server):
    fake = abs_server({"p1": {"name": "RL", "items": ["a", "b"]}})
    assert _set(["a", "b"]) == "unchanged"
    assert fake.writes() == []


def test_add_and_remove_then_patch(abs_server):
    fake = abs_server({"p1": {"name": "RL", "items": ["a", "b", "c"]}})
    assert _set(["d", "a", "c"]) == "updated"
    assert fake.writes() == [("POST", "/api/playlists/p1/batch/add"),
                             ("POST", "/api/playlists/p1/batch/remove"),
                             ("PATCH", "/api/playlists/p1")]
    assert fake.playlists["p1"]["items"] == ["d", "a", "c"]


def test_append_only_needs_no_patch(abs_server):
    fake = abs_server({"p1": {"name": "RL", "items": ["a"]}})
    _set(["a", "b"])
    assert fake.writes() == [("POST", "/api/playlists/p1/batch/add")]


def test_empty_without_allow_empty_changes_nothing(abs_server):
    fake = abs_server({"p1": {"name": "RL", "items": ["a"]}})
    assert _set([]) == "skipped"
    assert fake.writes() == []


def test_empty_with_allow_empty_reports_deleted(abs_server):
    fake = abs_server({"p1": {"name": "RL", "items": ["a"]}})
    assert _set([], allow_empty=True) == "deleted"
    assert "p1" not in fake.playlists


def test_missing_playlist(abs_server):
    fake = abs_server({})
    assert _set(["a"]) == "missing"
    assert fake.writes() == []


def test_push_queue_empty_queue_sends_no_writes(abs_server, test_db, monkeypatch):
    fake = abs_server({"p1": {"name": "RL", "items": ["a"]}})
    monkeypatch.setattr(sync, "ABS_URL", URL)
    monkeypatch.setattr(sync, "ABS_TOKEN", "tok")
    monkeypatch.setattr(sync, "ABS_PLAYLIST_ID", "p1")
    sync.push_queue_to_abs(1, reorder=True)
    assert fake.writes() == []


def _seed_pick(db, title, item_id, confidence):
    rec_id = db.upsert_recommendation(title, "A", None, "Book", "Yes", "r", confidence=confidence)
    with db.db() as conn:
        conn.execute("UPDATE recommendations SET in_abs_library = 1, abs_library_item_id = ? "
                     "WHERE id = ?", (item_id, rec_id))


def _picks(test_db, monkeypatch):
    _seed_pick(test_db, "One", "i1", 90)
    _seed_pick(test_db, "Two", "i2", 80)
    monkeypatch.setattr(sync, "_abs_library_id_for_item", lambda item_id: "lib1")


def test_picks_reuse_cached_playlist(abs_server, test_db, monkeypatch):
    _picks(test_db, monkeypatch)
    fake = abs_server({"pl9": {"name": "Bookclub Picks", "items": ["i2", "i1"]}})
    test_db.update_profile_picks_playlist_id(1, "pl9")
    assert sync.sync_picks_playlist(1, URL, "tok") == 2
    assert fake.writes() == [("PATCH", "/api/playlists/pl9")]
    assert fake.playlists["pl9"]["items"] == ["i1", "i2"]
    assert test_db.get_profile(1)["abs_picks_playlist_id"] == "pl9"


def test_picks_missing_cache_finds_by_name(abs_server, test_db, monkeypatch):
    _picks(test_db, monkeypatch)
    fake = abs_server({"other": {"name": "Reading List", "items": ["x"]},
                       "pl9": {"name": "Bookclub Picks", "items": ["i1"],
                               "description": sync.ABS_PICKS_DESCRIPTION}})
    test_db.update_profile_picks_playlist_id(1, "gone")
    assert sync.sync_picks_playlist(1, URL, "tok") == 2
    assert ("POST", "/api/playlists") not in fake.calls
    assert not any(m == "DELETE" for m, _ in fake.calls)
    assert fake.playlists["pl9"]["items"] == ["i1", "i2"]
    assert test_db.get_profile(1)["abs_picks_playlist_id"] == "pl9"


def test_picks_created_when_nothing_exists(abs_server, test_db, monkeypatch):
    _picks(test_db, monkeypatch)
    fake = abs_server({})
    assert sync.sync_picks_playlist(1, URL, "tok") == 2
    assert fake.writes() == [("POST", "/api/playlists")]
    assert fake.playlists["new-id"]["items"] == ["i1", "i2"]
    assert test_db.get_profile(1)["abs_picks_playlist_id"] == "new-id"


def test_picks_emptied_leaves_playlist_alone(abs_server, test_db, monkeypatch):
    fake = abs_server({"pl9": {"name": "Bookclub Picks", "items": ["i1"]}})
    test_db.update_profile_picks_playlist_id(1, "pl9")
    assert sync.sync_picks_playlist(1, URL, "tok") == 0
    assert fake.calls == []
    assert fake.playlists["pl9"]["items"] == ["i1"]
    assert test_db.get_profile(1)["abs_picks_playlist_id"] == "pl9"


def test_picks_adopts_only_playlist_with_our_description(abs_server, test_db, monkeypatch):
    _picks(test_db, monkeypatch)
    fake = abs_server({"mine": {"name": "Bookclub Picks", "items": ["i1"],
                                "description": sync.ABS_PICKS_DESCRIPTION}})
    assert sync.sync_picks_playlist(1, URL, "tok") == 2
    assert ("POST", "/api/playlists") not in fake.calls
    assert fake.playlists["mine"]["items"] == ["i1", "i2"]
    assert test_db.get_profile(1)["abs_picks_playlist_id"] == "mine"


def test_picks_does_not_adopt_foreign_playlist(abs_server, test_db, monkeypatch):
    _picks(test_db, monkeypatch)
    fake = abs_server({"theirs": {"name": "Bookclub Picks", "items": ["x"],
                                  "description": "my own list"}})
    assert sync.sync_picks_playlist(1, URL, "tok") == 2
    assert ("POST", "/api/playlists") in fake.calls
    assert fake.playlists["theirs"]["items"] == ["x"]
    assert fake.playlists["new-id"]["items"] == ["i1", "i2"]
    assert test_db.get_profile(1)["abs_picks_playlist_id"] == "new-id"


@pytest.fixture()
def abs_db(tmp_path, monkeypatch):
    """A tiny ABS SQLite holding the given library item ids."""
    import sqlite3
    path = tmp_path / "abs.sqlite"

    def make(ids):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE libraryItems (id TEXT PRIMARY KEY, libraryId TEXT)")
        conn.executemany("INSERT INTO libraryItems VALUES (?, 'lib1')", [(i,) for i in ids])
        conn.commit()
        conn.close()
        monkeypatch.setattr(sync, "ABS_DB_PATH", str(path))
    return make


def test_picks_skip_item_deleted_from_abs(abs_server, abs_db, test_db, monkeypatch):
    _picks(test_db, monkeypatch)
    abs_db(["i1"])
    fake = abs_server({}, known={"i1"})
    assert sync.sync_picks_playlist(1, URL, "tok") == 1
    assert fake.playlists["new-id"]["items"] == ["i1"]


def test_set_items_skips_stale_add(abs_server, abs_db):
    abs_db(["a", "b"])
    fake = abs_server({"p1": {"name": "RL", "items": ["a"]}}, known={"a", "b"})
    assert _set(["a", "gone", "b"]) == "updated"
    assert fake.playlists["p1"]["items"] == ["a", "b"]


# push_queue_to_abs: intent based pushes

@pytest.fixture()
def reading_list(abs_server, test_db, monkeypatch):
    monkeypatch.setattr(sync, "ABS_URL", URL)
    monkeypatch.setattr(sync, "ABS_TOKEN", "tok")
    monkeypatch.setattr(sync, "ABS_PLAYLIST_ID", "p1")

    def setup(abs_items, queued, known=None):
        fake = abs_server({"p1": {"name": "RL", "items": list(abs_items)}}, known=known)
        recs = {}
        for item in ["a", "b", "c", "d", "x"]:
            recs[item] = test_db.upsert_recommendation(item.upper(), "A", None, "Book", "Yes", "r")
            with test_db.db() as conn:
                conn.execute("UPDATE recommendations SET abs_library_item_id = ? WHERE id = ?",
                             (item, recs[item]))
        for item in queued:
            test_db.add_to_queue(recs[item], 1)
        return fake, recs
    return setup


def test_push_add_keeps_abs_only_item(reading_list):
    fake, recs = reading_list(["a", "x"], ["a", "b"])
    assert sync.push_queue_to_abs(1, add=[recs["b"]]) is True
    assert fake.writes() == [("POST", "/api/playlists/p1/batch/add")]
    assert fake.playlists["p1"]["items"] == ["a", "x", "b"]


def test_push_remove_touches_only_that_item(reading_list, test_db):
    fake, recs = reading_list(["a", "b", "x"], ["a", "b"])
    test_db.remove_from_queue(recs["b"], 1)
    assert sync.push_queue_to_abs(1, remove=[recs["b"]]) is True
    assert fake.writes() == [("POST", "/api/playlists/p1/batch/remove")]
    assert fake.playlists["p1"]["items"] == ["a", "x"]


def test_push_remove_last_item_does_not_empty_playlist(reading_list, test_db):
    fake, recs = reading_list(["a"], ["a"])
    test_db.remove_from_queue(recs["a"], 1)
    sync.push_queue_to_abs(1, remove=[recs["a"]])
    assert fake.writes() == []
    assert fake.playlists["p1"]["items"] == ["a"]


def test_push_reorder_keeps_abs_only_item_after(reading_list):
    fake, recs = reading_list(["x", "a", "b", "c"], ["c", "b", "a"])
    assert sync.push_queue_to_abs(1, reorder=True) is True
    assert fake.writes() == [("PATCH", "/api/playlists/p1")]
    assert fake.playlists["p1"]["items"] == ["c", "b", "a", "x"]


def test_push_add_of_item_deleted_from_abs(reading_list, abs_db):
    abs_db(["a"])
    fake, recs = reading_list(["a"], ["a", "b"], known={"a"})
    assert sync.push_queue_to_abs(1, add=[recs["b"]]) is True
    assert fake.writes() == []
    assert fake.playlists["p1"]["items"] == ["a"]


def test_push_add_undone_before_it_runs(reading_list):
    fake, recs = reading_list(["a"], ["a"])
    assert sync.push_queue_to_abs(1, add=[recs["b"]]) is False
    assert fake.writes() == []


def test_push_reads_queue_at_run_time(reading_list, test_db):
    fake, recs = reading_list(["a", "b"], ["a", "b"])
    test_db.reorder_queue([recs["b"], recs["a"]], 1)
    sync.push_queue_to_abs(1, reorder=True)
    assert fake.playlists["p1"]["items"] == ["b", "a"]
