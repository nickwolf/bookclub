from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import gen


def _model(mid, day, structured=True, name=None):
    return NS(id=mid, display_name=name or mid, created_at=datetime(2026, 1, day),
              capabilities={"structured_outputs": {"supported": structured}})


class FakeModels:
    def __init__(self, models, exc=None):
        self.models, self.exc = models, exc

    def list(self):
        if self.exc:
            raise self.exc
        return iter(self.models)


def _fake(monkeypatch, models, exc=None):
    monkeypatch.setattr(gen, "_client", lambda: NS(models=FakeModels(models, exc)))


MODELS = [
    _model("claude-sonnet-5-5", 10, name="Claude Sonnet 5.5"),
    _model("claude-sonnet-5", 5, name="Claude Sonnet 5"),
    _model("claude-opus-5", 20, name="Claude Opus 5"),
    _model("claude-sonnet-9-legacy", 25, structured=False, name="Old Sonnet"),
    _model("claude-haiku-5", 8, structured=False, name="Claude Haiku 5"),
]


def test_refresh_replaces_cache(test_db, monkeypatch):
    _fake(monkeypatch, MODELS)
    assert gen.refresh_models() == 5
    _fake(monkeypatch, MODELS[:2])
    assert gen.refresh_models() == 2
    cached = test_db.get_cached_models()
    assert [m["id"] for m in cached] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert cached[0]["structured_outputs"] == 1
    assert cached[0]["created_at"].startswith("2026-01-10")


def test_refresh_error_keeps_cache(test_db, monkeypatch):
    _fake(monkeypatch, MODELS)
    gen.refresh_models()
    _fake(monkeypatch, [], exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        gen.refresh_models()
    assert len(test_db.get_cached_models()) == 5


def test_resolve_model_precedence(test_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert gen.resolve_model() == gen.DEFAULT_MODEL
    _fake(monkeypatch, MODELS + [_model("claude-sonnet-6", 15)])
    gen.refresh_models()
    # newest structured-output Sonnet wins; the newer non-structured one and Opus are skipped
    assert gen.resolve_model() == "claude-sonnet-6"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env")
    assert gen.resolve_model() == "claude-env"
    test_db.set_setting("model", "claude-opus-5")
    assert gen.resolve_model() == "claude-opus-5"
    test_db.set_setting("model", "")
    assert gen.resolve_model() == "claude-env"
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    assert gen.resolve_model() == "claude-sonnet-6"


def test_resolve_model_falls_back_without_sonnet(test_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    _fake(monkeypatch, [_model("claude-opus-5", 1)])
    gen.refresh_models()
    assert gen.resolve_model() == gen.DEFAULT_MODEL


def test_startup_refresh_only_with_key_and_empty_cache(test_db, monkeypatch):
    calls = []
    monkeypatch.setattr(gen, "refresh_models", lambda: calls.append(1))
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "")
    gen.refresh_models_if_empty()
    assert not calls
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "k")
    gen.refresh_models_if_empty()
    assert calls == [1]
    monkeypatch.setattr(gen, "refresh_models", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    gen.refresh_models_if_empty()  # swallowed


def test_route_save_model(client, test_db):
    r = client.post("/settings/model", data={"model": "claude-x"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Saved" in r.text
    assert test_db.get_setting("model") == "claude-x"
    client.post("/settings/model", data={"model": ""})
    assert test_db.get_setting("model") is None


def test_route_refresh(client, test_db, monkeypatch):
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "k")
    _fake(monkeypatch, MODELS)
    r = client.post("/settings/models/refresh")
    assert "Fetched 5 models" in r.text
    _fake(monkeypatch, [], exc=RuntimeError("bad key"))
    r = client.post("/settings/models/refresh")
    assert "Could not fetch models: bad key" in r.text


def test_route_refresh_without_key(client, monkeypatch):
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "")
    r = client.post("/settings/models/refresh")
    assert "ANTHROPIC_API_KEY is not set" in r.text


def test_picker_lists_only_structured_models(client, test_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    _fake(monkeypatch, MODELS)
    gen.refresh_models()
    html = client.get("/settings").text
    assert "Default: latest Sonnet (claude-sonnet-5-5)" in html
    assert "Claude Opus 5" in html and "Claude Sonnet 5.5" in html
    assert "Claude Haiku 5" not in html and "Old Sonnet" not in html
    assert html.index("Claude Opus 5") < html.index("Claude Sonnet 5.5") < html.index("Claude Sonnet 5</option>")


def test_picker_empty_state(client, monkeypatch):
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "")
    assert "no API key is configured" in client.get("/settings").text
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "k")
    assert "No models cached yet" in client.get("/settings").text


def test_generate_page_shows_model(client, test_db, monkeypatch):
    monkeypatch.setattr(gen, "ANTHROPIC_API_KEY", "k")
    test_db.set_setting("model", "claude-saved")
    assert "claude-saved" in client.get("/recs/refresh").text


def test_resolve_model_skips_saved_without_structured(test_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    _fake(monkeypatch, MODELS)
    gen.refresh_models()
    test_db.set_setting("model", "claude-haiku-5")
    assert gen.resolve_model(warn=True) == "claude-sonnet-5-5"
    with test_db.db() as conn:
        logged = conn.execute(
            "SELECT COUNT(*) FROM app_log WHERE message LIKE '%claude-haiku-5%'").fetchone()[0]
    assert logged == 1


def test_resolve_model_skips_saved_missing_from_cache(test_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    test_db.set_setting("model", "claude-unlisted")
    # an empty cache cannot say anything, so the saved model is kept
    assert gen.resolve_model() == "claude-unlisted"
    _fake(monkeypatch, MODELS)
    gen.refresh_models()
    assert gen.resolve_model(warn=True) == "claude-sonnet-5-5"
    with test_db.db() as conn:
        logged = conn.execute(
            "SELECT COUNT(*) FROM app_log WHERE message LIKE '%claude-unlisted%'").fetchone()[0]
    assert logged == 1


def test_route_rejects_non_structured_model(client, test_db):
    _fake_cache = [{"id": "claude-haiku-5", "display_name": "H", "created_at": "2026-01-01",
                    "structured_outputs": False}]
    test_db.replace_models_cache(_fake_cache)
    r = client.post("/settings/model", data={"model": "claude-haiku-5"})
    assert r.status_code == 400 and "does not support structured outputs" in r.text
    assert test_db.get_setting("model") is None
    assert client.post("/settings/model", data={"model": "claude-unlisted"}).status_code == 200
