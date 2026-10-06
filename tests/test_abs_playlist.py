import json

import httpx
import pytest

import sync

URL = "http://abs.test"


class FakeAbs:
    """Stateful stand-in for the ABS playlist endpoints; records every call."""

    def __init__(self, playlists=None):
        self.playlists = playlists or {}  # id -> {"name": str, "items": [ids]}
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path))
        if request.method == "GET" and path == "/api/playlists":
            return httpx.Response(200, json={"playlists": [
                {"id": i, "name": p["name"], "items": []} for i, p in self.playlists.items()]})
        if request.method == "POST" and path == "/api/playlists":
            pid = "new-id"
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
    def install(playlists=None):
        fake = FakeAbs(playlists)
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
    assert sync.push_queue_to_abs(1) is False
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
                       "pl9": {"name": "Bookclub Picks", "items": ["i1"]}})
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


def test_picks_emptied_clears_cached_id(abs_server, test_db, monkeypatch):
    fake = abs_server({"pl9": {"name": "Bookclub Picks", "items": ["i1"]}})
    test_db.update_profile_picks_playlist_id(1, "pl9")
    assert sync.sync_picks_playlist(1, URL, "tok") == 0
    assert "pl9" not in fake.playlists
    assert test_db.get_profile(1)["abs_picks_playlist_id"] is None
