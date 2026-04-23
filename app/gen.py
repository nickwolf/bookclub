"""
In-app recommendation generation using the Anthropic API.
Mirrors the logic in refresh_recs.py but runs inside the container.
"""

import json
import os
import re

import anthropic

import db

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def api_key_configured() -> bool:
    return bool(ANTHROPIC_API_KEY)


def build_prompt(ctx: dict, count: int) -> str:
    profile_name = ctx["profile_name"]
    sections = [f"You are recommending books for {profile_name}."]

    if ctx.get("preferences"):
        sections.append(f"Their stated reading preferences:\n  {ctx['preferences']}")

    if ctx.get("want_to_read"):
        lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'}"
            for b in ctx["want_to_read"][:100]
        )
        sections.append(
            f"Books already on their Want-to-Read list (do NOT recommend these — "
            f"they've already found them, but use as taste signal):\n{lines}"
        )

    if ctx.get("currently_reading"):
        lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'}"
            + (f" (series: {b['series']})" if b.get("series") else "")
            for b in ctx["currently_reading"]
        )
        sections.append(
            f"Currently reading (do not recommend sequels they'll get to naturally):\n{lines}"
        )

    top_books = ctx["top_rated_books"][:60]
    top_str = "\n".join(
        f"  - {b['title']} by {b.get('author') or 'Unknown'}"
        + (f" (series: {b['series']})" if b.get("series") else "")
        + f" — {b['rating']}★"
        for b in top_books
    )
    sections.append(f"Their highest-rated books from Hardcover (4–5 stars):\n{top_str}")

    if ctx.get("passed_with_notes"):
        lines = "\n".join(
            f"  - {r['title']}: {r['user_notes']}" for r in ctx["passed_with_notes"]
        )
        sections.append(f"Recommendations they passed on and why:\n{lines}")

    if ctx.get("read_recs"):
        lines = "\n".join(
            f"  - {r['title']}"
            + (f" ({r['user_rating']}★)" if r.get("user_rating") else "")
            + (f" — {r['user_notes']}" if r.get("user_notes") else "")
            for r in ctx["read_recs"]
        )
        sections.append(f"Recommendations they've already read and rated:\n{lines}")

    if ctx.get("dnf_books"):
        lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'}"
            for b in ctx["dnf_books"]
        )
        sections.append(
            f"Books they did not finish (avoid recommending similar — "
            f"pay attention to what these have in common):\n{lines}"
        )

    if ctx.get("low_rated_books"):
        lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'} — {b['rating']}★"
            for b in ctx["low_rated_books"]
        )
        sections.append(
            f"Books they finished but rated poorly (1–2 stars) — stronger negative signal "
            f"than DNF, pay close attention to what these have in common:\n{lines}"
        )

    existing = [r["title"] for r in ctx.get("existing_recs", [])]
    if existing:
        sections.append(f"Already recommended — do NOT repeat these:\n  {', '.join(existing)}")

    sections += [
        "",
        f"Generate exactly {count} NEW book or series recommendations they have not read "
        f"and are not listed above.",
        "Focus on finding logical next-reads and gaps given their taste profile.",
        "",
        "Include a 'confidence' integer (0–100) for how confident you are this specific "
        "recommendation fits their taste based on their reading history. Be precise — "
        "reserve 90+ for near-certain fits, use 60–79 for reasonable bets.",
        "",
        "IMPORTANT: The 'reason' field must contain only 1-2 sentences explaining why this "
        "book fits the user's taste. Never put any meta-commentary, corrections, or notes "
        "about the recommendation process in the 'reason' field.",
        "IMPORTANT: Do NOT include any book already listed above. If you catch yourself "
        "about to include a duplicate, silently skip it and pick a different book instead. "
        "Never mention duplicates or corrections in your output — just produce the final list.",
        "",
        "Respond with ONLY a JSON array, no preamble, no explanation, no markdown fences:",
        '[{"title":"...","author":"...","series":"... or null","type":"Book or Series",'
        '"audiobook_available":"Yes, No, or Partial","confidence":85,'
        '"reason":"1-2 sentences why this fits their taste","tags":["genre","subgenre","theme"]}]',
    ]
    return "\n\n".join(sections)


def extract_json(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON array found in Claude's response")
    return json.loads(match.group(0))


def run_generation(profile_id: int, count: int) -> dict:
    """
    Synchronous — intended to run in a background thread.
    Returns {"added": N} on success, raises on failure.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env and restart the container."
        )

    db.log("gen", f"Generation started — requesting {count} recs (model: {ANTHROPIC_MODEL})")

    ctx = db.get_rec_context(profile_id)
    prompt = build_prompt(ctx, count)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        db.log("gen", f"Claude API call failed: {e}", level="error")
        raise
    recs = extract_json(message.content[0].text)

    # Deduplicate within the returned batch (case-insensitive) and against existing DB titles
    with db.db() as conn:
        existing_titles = {
            row[0].lower() for row in
            conn.execute("SELECT title FROM recommendations").fetchall()
        }
    seen_in_batch: set[str] = set()
    deduped = []
    for rec in recs:
        key = rec.get("title", "").strip().lower()
        if not key:
            continue
        if key in existing_titles or key in seen_in_batch:
            db.log("gen", f"Skipped duplicate: {rec.get('title')}", level="info")
            continue
        seen_in_batch.add(key)
        deduped.append(rec)
    recs = deduped

    added = 0
    cover_targets: list[tuple[int, str, str]] = []
    for rec in recs:
        tags_list = rec.get("tags") or []
        tags = ", ".join(tags_list) if isinstance(tags_list, list) else tags_list or None
        raw_conf = rec.get("confidence")
        confidence = max(0, min(100, int(raw_conf))) if isinstance(raw_conf, (int, float)) else None
        rec_id = db.upsert_recommendation(
            rec["title"], rec.get("author"), rec.get("series"),
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

    db.log("gen", f"Generation complete — added {added} recommendations")
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
