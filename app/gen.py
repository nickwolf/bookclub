"""
In-app recommendation generation using the Anthropic API.
Mirrors the logic in refresh_recs.py but runs inside the container.
"""

import json
import os

import anthropic

import db
from textnorm import filter_duplicates

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL = "claude-sonnet-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                    "series": {"type": "string"},
                    "type": {"type": "string", "enum": ["Book", "Series"]},
                    "audiobook_available": {"type": "string", "enum": ["Yes", "No", "Partial"]},
                    "confidence": {"type": "integer"},
                    "reason": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "author", "series", "type", "audiobook_available",
                             "confidence", "reason", "tags"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


def model_lacks_structured_outputs(model_id: str) -> bool:
    """True only when the cache knows the model and says it can't do structured outputs."""
    return any(m["id"] == model_id and not m["structured_outputs"] for m in db.get_cached_models())


def resolve_model(warn: bool = False) -> str:
    """Saved choice, then ANTHROPIC_MODEL, then newest cached Sonnet, then DEFAULT_MODEL."""
    saved = db.get_setting("model")
    if saved:
        cache = {m["id"]: m for m in db.get_cached_models()}
        if cache and saved not in cache:
            problem = "is not in the model list"
        elif model_lacks_structured_outputs(saved):
            problem = "lacks structured outputs"
        else:
            problem = None
        if problem:
            if warn:
                db.log("gen", f"Saved model {saved} {problem}, using the default",
                       level="warning")
            saved = None
    return saved or os.environ.get("ANTHROPIC_MODEL") or latest_sonnet()


def latest_sonnet() -> str:
    """Newest cached Sonnet that supports structured outputs, else DEFAULT_MODEL."""
    for m in db.get_cached_models(structured_only=True):
        if m["id"].startswith("claude-sonnet-"):
            return m["id"]
    return DEFAULT_MODEL


def refresh_models() -> int:
    """Pull the model list from the API into the cache. Raises on API error."""
    models = []
    for m in _client().models.list():
        caps = getattr(m, "capabilities", None) or {}
        models.append({
            "id": m.id,
            "display_name": m.display_name or m.id,
            "created_at": m.created_at.isoformat(),
            "structured_outputs": bool(((caps.get("structured_outputs") or {}).get("supported"))),
        })
    n = db.replace_models_cache(models)
    db.log("gen", f"Refreshed model list ({n} models)")
    return n


def refresh_models_if_empty():
    """Startup helper: best-effort fill of an empty cache."""
    if not api_key_configured() or db.get_cached_models():
        return
    try:
        refresh_models()
    except Exception as e:
        db.log("gen", f"Model list refresh failed: {e!r}", level="warning")


def _client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def api_key_configured() -> bool:
    return bool(ANTHROPIC_API_KEY)


def build_prompt(ctx: dict, count: int) -> str:
    profile_name = ctx["profile_name"]
    sections = [f"You are recommending books for {profile_name}."]

    if ctx.get("preferences"):
        sections.append(f"Their stated reading preferences:\n  {ctx['preferences']}")

    def _fmt(b, rating=False):
        line = f"  - {b['title']} by {b.get('author') or 'Unknown'}"
        if b.get("series"):
            line += f" (series: {b['series']})"
        if rating:
            line += f" ({b['rating']}★)"
        return line

    def _section(header, books, rating=False):
        if books:
            sections.append(f"{header}\n" + "\n".join(_fmt(b, rating) for b in books))

    _section("Books they loved (rated 4-5 stars), the strongest taste signal:",
             ctx.get("top_rated_books", []), rating=True)
    _section("Books they finished but disliked (rated 1-2 stars), a strong negative "
             "signal, so pay close attention to what these have in common:",
             ctx.get("low_rated_books", []), rating=True)
    _section("Other books they have read (rated 3 stars or not rated):",
             ctx.get("other_read_books", []))
    _section("Books they did not finish (avoid recommending similar, and pay attention "
             "to what these have in common):", ctx.get("dnf_books", []))
    _section("Books they started but paused (do not recommend these):",
             ctx.get("paused_books", []))
    _section("Currently reading (do not recommend sequels they'll get to naturally):",
             ctx.get("currently_reading", []))
    _section("Their Want-to-Read list (do not recommend these, they have already found "
             "them, but use them as a taste signal):", ctx.get("want_to_read", []))

    if ctx.get("passed_with_notes"):
        lines = "\n".join(
            f"  - {r['title']}: {r['user_notes']}" for r in ctx["passed_with_notes"]
        )
        sections.append(f"Recommendations they passed on and why:\n{lines}")

    if ctx.get("read_recs"):
        lines = "\n".join(
            f"  - {r['title']}"
            + (f" ({r['user_rating']}★)" if r.get("user_rating") else "")
            + (f": {r['user_notes']}" if r.get("user_notes") else "")
            for r in ctx["read_recs"]
        )
        sections.append(f"Recommendations they've already read and rated:\n{lines}")

    existing = [r["title"] for r in ctx.get("existing_recs", [])]
    if existing:
        sections.append("Already recommended:\n" + "\n".join(f"  - {t}" for t in existing))

    sections.append(
        "Never recommend any book or series that appears in any list above, in any "
        "format (audiobook, ebook, or print)."
    )

    sections += [
        "",
        f"Generate exactly {count} NEW book or series recommendations they have not read "
        f"and are not listed above.",
        "Focus on finding logical next-reads and gaps given their taste profile.",
        "",
        "Include a 'confidence' integer (0-100) for how confident you are this specific "
        "recommendation fits their taste based on their reading history. Be precise: "
        "reserve 90+ for near-certain fits, use 60-79 for reasonable bets.",
        "",
        "IMPORTANT: The 'reason' field must contain only 1-2 sentences explaining why this "
        "book fits the user's taste. Never put any meta-commentary, corrections, or notes "
        "about the recommendation process in the 'reason' field.",
        "IMPORTANT: Do NOT include any book already listed above. If you catch yourself "
        "about to include a duplicate, silently skip it and pick a different book instead. "
        "Never mention duplicates or corrections in your output, just produce the final list.",
        "",
        "For each recommendation give the title, author, series (empty if none), type "
        "(Book or Series), audiobook availability, confidence, reason, and a few tags "
        "(genre, subgenre, theme).",
    ]
    return "\n\n".join(sections)


def _parse_response(message) -> list:
    """Check stop_reason, then parse the first text block as the schema JSON."""
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "The response was truncated before it finished. "
            "Try requesting fewer recommendations."
        )
    if message.stop_reason == "refusal":
        sd = getattr(message, "stop_details", None)
        detail = " ".join(
            x for x in (getattr(sd, "category", None), getattr(sd, "explanation", None)) if x
        )
        raise RuntimeError("Claude declined this request" + (f": {detail}" if detail else "."))
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise RuntimeError("Claude returned no text content.")
    try:
        return json.loads(text)["recommendations"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"Claude returned malformed output ({e}).") from e


def _api_failed(exc: Exception, friendly: str):
    db.log("gen", f"Claude API call failed: {exc!r}", level="error")
    raise RuntimeError(friendly) from exc


def run_generation(profile_id: int, count: int) -> dict:
    """
    Synchronous, intended to run in a background thread.
    Returns {"added": N} on success, raises on failure.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env and restart the container."
        )

    model = resolve_model(warn=True)
    db.log("gen", f"Generation started, requesting {count} recs (model: {model})")

    ctx = db.get_rec_context(profile_id)
    prompt = build_prompt(ctx, count)

    try:
        with _client().messages.stream(
            model=model,
            max_tokens=32000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        _api_failed(e, "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.")
    except anthropic.NotFoundError as e:
        _api_failed(e, f"Model '{model}' was not found. Refresh the model list or pick another model.")
    except anthropic.RateLimitError as e:
        _api_failed(e, "Anthropic rate limit reached. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        _api_failed(e, f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        _api_failed(e, "Could not reach the Anthropic API. Check the network and try again.")

    usage = message.usage
    db.log("gen", f"Claude responded (model: {model}, input_tokens: {usage.input_tokens}, "
                  f"output_tokens: {usage.output_tokens})")
    try:
        recs = _parse_response(message)
    except RuntimeError as e:
        db.log("gen", f"Bad Claude response: {e}", level="error")
        raise

    # Deduplicate against the catalog, all HC books, and the batch itself
    blocked_keys, blocked_series = db.get_blocked_title_keys()
    recs, skipped = filter_duplicates(recs, blocked_keys, blocked_series)
    for rec in skipped:
        db.log("gen", f"Skipped duplicate/already-read: {rec.get('title')}", level="info")

    added = 0
    cover_targets: list[tuple[int, str, str]] = []
    for rec in recs:
        tags_list = rec.get("tags") or []
        tags = ", ".join(tags_list) if isinstance(tags_list, list) else tags_list or None
        raw_conf = rec.get("confidence")
        confidence = max(0, min(100, int(raw_conf))) if isinstance(raw_conf, (int, float)) else None
        rec_id = db.upsert_recommendation(
            rec["title"], rec.get("author"), rec.get("series") or None,
            rec.get("type", "Book"), rec.get("audiobook_available", "Unknown"),
            rec.get("reason"), source="claude-api", tags=tags, confidence=confidence,
        )
        with db.db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rec_interactions (profile_id, rec_id, user_status) "
                "VALUES (?, ?, 'pending')",
                (profile_id, rec_id),
            )
        cover_targets.append((rec_id, rec.get("title", ""), rec.get("author", "")))
        added += 1

    # Fetch Open Library covers synchronously (best-effort)
    _fetch_covers_sync(cover_targets)

    db.log("gen", f"Generation complete, added {added} recommendations")
    return {"added": added}


def _fetch_covers_sync(recs: list[tuple[int, str, str]]):
    import httpx
    with httpx.Client(timeout=8) as client:
        for rec_id, title, author in recs:
            try:
                params = {"title": title, "limit": 1, "fields": "cover_i"}
                if author:
                    params["author"] = author
                resp = client.get("https://openlibrary.org/search.json", params=params)
                docs = resp.json().get("docs", [])
                if docs and docs[0].get("cover_i"):
                    cover_url = f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-M.jpg"
                    db.update_rec_cover(rec_id, cover_url)
            except Exception:
                pass
