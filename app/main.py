import os
import threading
from datetime import datetime
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

import db
import gen as generator
import sync as syncer
from sync import push_queue_to_abs

app = FastAPI(title="Bookclub")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

_sync_lock = threading.Lock()
_sync_running = False

_gen_lock = threading.Lock()
_gen_running = False
_gen_last: dict | None = None  # {"added": N, "error": "...", "finished_at": "..."}


def _run_gen(profile_id: int, count: int):
    global _gen_running, _gen_last
    _gen_running = True
    try:
        result = generator.run_generation(profile_id, count)
        _gen_last = {**result, "error": None, "finished_at": datetime.now().isoformat()}
    except Exception as exc:
        db.log("gen", f"Generation error: {exc}", level="error")
        _gen_last = {"added": 0, "error": str(exc), "finished_at": datetime.now().isoformat()}
    finally:
        _gen_running = False
        _gen_lock.release()


@app.on_event("startup")
def startup():
    db.init_db()
    syncer.seed_if_empty()
    # Auto-sync on startup if last sync was more than 1 hour ago (or never)
    if _should_auto_sync():
        t = threading.Thread(target=_run_sync, args=(1,), daemon=True)
        t.start()


def _should_auto_sync() -> bool:
    last = db.get_last_sync()
    if not last or last["status"] != "ok":
        return True
    try:
        last_time = datetime.fromisoformat(last["finished_at"])
        return (datetime.now() - last_time).total_seconds() > 3600
    except (ValueError, TypeError):
        return True


def _run_sync(profile_id: int = 1):
    global _sync_running
    if not _sync_lock.acquire(blocking=False):
        return
    _sync_running = True
    try:
        syncer.run_full_sync(profile_id)
    finally:
        _sync_running = False
        _sync_lock.release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    "all":        "All",
    "pending":    "Pending",
    "queued":     "Queued",
    "in_library": "In Library",
    "archive":    "Archive",
}

HC_STATUS = {1: "Want to Read", 2: "Reading", 3: "Read", 4: "DNF"}


def get_profile_id(request: Request) -> int:
    try:
        return int(request.cookies.get("profile_id", "1"))
    except (ValueError, TypeError):
        return 1


def _tmpl(request, name, **ctx):
    profile_id = get_profile_id(request)
    ctx["request"] = request
    ctx["stats"] = db.get_stats(profile_id)
    ctx["last_sync"] = db.get_last_sync()
    ctx["active_profile"] = db.get_profile(profile_id)
    ctx["all_profiles"] = db.get_profiles()
    return templates.TemplateResponse(name, ctx)


def _card(request, rec_id: int):
    profile_id = get_profile_id(request)
    rec = db.get_recommendation(rec_id, profile_id)
    return templates.TemplateResponse("partials/rec_card.html",
                                      {"request": request, "rec": rec})


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def recommendations_page(request: Request, status: str = "all", q: str = ""):
    profile_id = get_profile_id(request)
    recs = db.get_recommendations(status, profile_id, q)
    return _tmpl(request, "recommendations.html",
                 recs=recs, current_status=status, q=q, status_labels=STATUS_LABELS)


@app.post("/rec/{rec_id}/queue")
def rec_queue(rec_id: int, request: Request, background_tasks: BackgroundTasks):
    profile_id = get_profile_id(request)
    db.add_to_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    if request.headers.get("HX-Request"):
        return _card(request, rec_id)
    return RedirectResponse("/", status_code=303)


@app.post("/rec/{rec_id}/pass")
def rec_pass(rec_id: int, request: Request, background_tasks: BackgroundTasks):
    profile_id = get_profile_id(request)
    db.set_rec_status(rec_id, "pass", profile_id)
    db.remove_from_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    if request.headers.get("HX-Request"):
        return _card(request, rec_id)
    return RedirectResponse("/", status_code=303)


@app.post("/rec/{rec_id}/unpass")
def rec_unpass(rec_id: int, request: Request):
    db.set_rec_status(rec_id, "pending", get_profile_id(request))
    if request.headers.get("HX-Request"):
        return _card(request, rec_id)
    return RedirectResponse("/", status_code=303)


@app.post("/rec/{rec_id}/read")
def rec_mark_read(rec_id: int, request: Request, background_tasks: BackgroundTasks,
                  source: str = Form("")):
    profile_id = get_profile_id(request)
    db.set_rec_status(rec_id, "read", profile_id)
    db.remove_from_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    if request.headers.get("HX-Request"):
        if source == "queue":
            items = db.get_queue(profile_id)
            return templates.TemplateResponse("partials/queue_list.html",
                                              {"request": request, "items": items})
        return _card(request, rec_id)
    return RedirectResponse("/", status_code=303)


@app.post("/rec/{rec_id}/unread")
def rec_unread(rec_id: int, request: Request):
    profile_id = get_profile_id(request)
    db.set_rec_status(rec_id, "pending", profile_id)
    db.set_rec_rating(rec_id, None, profile_id)
    if request.headers.get("HX-Request"):
        return _card(request, rec_id)
    return RedirectResponse("/", status_code=303)


@app.post("/rec/{rec_id}/rate")
def rec_rate(rec_id: int, request: Request, rating: int = Form(...), source: str = Form("")):
    db.set_rec_rating(rec_id, rating, get_profile_id(request))
    if request.headers.get("HX-Request"):
        if source == "queue":
            stars = "★" * rating + "☆" * (5 - rating)
            return HTMLResponse(f'<div id="rec-rate-{rec_id}" class="rate-done">{stars}</div>')
        return _card(request, rec_id)
    return RedirectResponse("/", status_code=303)


@app.post("/rec/{rec_id}/note")
def rec_note(rec_id: int, request: Request, notes: str = Form("")):
    db.set_rec_notes(rec_id, notes, get_profile_id(request))
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="save-ok">Saved ✓</span>')
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Queue / Playlist
# ---------------------------------------------------------------------------

@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    items = db.get_queue(get_profile_id(request))
    return _tmpl(request, "queue.html", items=items)


@app.post("/queue/{rec_id}/remove")
def queue_remove(rec_id: int, request: Request, background_tasks: BackgroundTasks):
    profile_id = get_profile_id(request)
    db.remove_from_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    if request.headers.get("HX-Request"):
        items = db.get_queue(profile_id)
        return templates.TemplateResponse("partials/queue_list.html",
                                          {"request": request, "items": items})
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{queue_id}/move/{direction}")
def queue_move(queue_id: int, direction: str, request: Request, background_tasks: BackgroundTasks):
    profile_id = get_profile_id(request)
    db.move_queue_item(queue_id, direction, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    if request.headers.get("HX-Request"):
        items = db.get_queue(profile_id)
        return templates.TemplateResponse("partials/queue_list.html",
                                          {"request": request, "items": items})
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/reorder")
async def queue_reorder(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    profile_id = get_profile_id(request)
    rec_ids = [int(v) for v in form.getlist("rec_ids[]")]
    db.reorder_queue(rec_ids, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    return HTMLResponse("", status_code=200)


# ---------------------------------------------------------------------------
# History (Hardcover read books)
# ---------------------------------------------------------------------------

HISTORY_PAGE_SIZE = 50

@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, q: str = "", rating: str = "", page: int = 1):
    page = max(1, page)
    offset = (page - 1) * HISTORY_PAGE_SIZE
    with db.db() as conn:
        where_clauses = ["status_id = 3"]
        params: list = []
        if q:
            where_clauses.append("(lower(title) LIKE ? OR lower(author) LIKE ?)")
            params += [f"%{q.lower()}%", f"%{q.lower()}%"]
        if rating:
            where_clauses.append("rating >= ?")
            params.append(float(rating))
        where = " AND ".join(where_clauses)
        total = conn.execute(
            f"SELECT COUNT(*) FROM hc_books WHERE {where}", params
        ).fetchone()[0]
        books = conn.execute(
            f"SELECT * FROM hc_books WHERE {where} ORDER BY rating DESC NULLS LAST, title LIMIT ? OFFSET ?",
            params + [HISTORY_PAGE_SIZE, offset]
        ).fetchall()
    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    return _tmpl(request, "history.html", books=books, q=q, rating=rating, hc_status=HC_STATUS,
                 page=page, total_pages=total_pages, total=total)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request):
    return _tmpl(request, "profiles.html")


@app.post("/profiles")
def create_profile_route(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        return RedirectResponse("/profiles", status_code=303)
    pid = db.create_profile(name)
    resp = RedirectResponse("/profiles", status_code=303)
    resp.set_cookie("profile_id", str(pid), max_age=365 * 24 * 3600)
    return resp


@app.post("/profiles/switch/{profile_id}")
def switch_profile(profile_id: int, request: Request):
    referer = request.headers.get("referer", "/")
    resp = RedirectResponse(referer, status_code=303)
    resp.set_cookie("profile_id", str(profile_id), max_age=365 * 24 * 3600)
    return resp


@app.post("/profiles/{profile_id}/preferences")
def save_preferences(profile_id: int, request: Request, preferences: str = Form("")):
    db.update_profile_preferences(profile_id, preferences)
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="save-ok">Saved ✓</span>')
    return RedirectResponse("/profiles", status_code=303)


@app.post("/profiles/{profile_id}/abs-token")
def save_abs_token(profile_id: int, request: Request, abs_token: str = Form("")):
    db.update_profile_abs_token(profile_id, abs_token)
    # Always clear cached playlist ID — re-resolved on next sync
    db.update_profile_picks_playlist_id(profile_id, None)
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="save-ok">Saved ✓</span>')
    return RedirectResponse("/profiles", status_code=303)


# ---------------------------------------------------------------------------
# Review queue (Discovery-style triage)
# ---------------------------------------------------------------------------

def _parse_skip_ids(skip_ids: str) -> list[int]:
    return [int(x) for x in skip_ids.split(",") if x.strip().isdigit()]


def _review_card(request: Request, skip_ids: str, rate_mode: bool = False):
    profile_id = get_profile_id(request)
    skip_list = _parse_skip_ids(skip_ids)
    rec = db.get_next_review_rec(profile_id, skip_list)
    stats = db.get_stats(profile_id)
    remaining = stats["pending"] + stats["unrated_recs"]
    return templates.TemplateResponse("partials/review_card.html", {
        "request": request, "rec": rec,
        "skip_ids": skip_ids, "rate_mode": rate_mode, "remaining": remaining,
    })


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, skip_ids: str = ""):
    profile_id = get_profile_id(request)
    stats = db.get_stats(profile_id)
    remaining = stats["pending"] + stats["unrated_recs"]
    skip_list = _parse_skip_ids(skip_ids)
    rec = db.get_next_review_rec(profile_id, skip_list)
    return _tmpl(request, "review.html",
                 rec=rec, skip_ids=skip_ids, remaining=remaining, rate_mode=False)


@app.post("/review/{rec_id}/queue")
def review_queue(rec_id: int, request: Request, background_tasks: BackgroundTasks,
                 skip_ids: str = Form("")):
    profile_id = get_profile_id(request)
    db.add_to_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    return _review_card(request, skip_ids)


@app.post("/review/{rec_id}/pass")
def review_pass(rec_id: int, request: Request, background_tasks: BackgroundTasks,
                skip_ids: str = Form("")):
    profile_id = get_profile_id(request)
    db.set_rec_status(rec_id, "pass", profile_id)
    db.remove_from_queue(rec_id, profile_id)
    background_tasks.add_task(push_queue_to_abs, profile_id)
    return _review_card(request, skip_ids)


@app.get("/review/{rec_id}/read-confirm")
def review_read_confirm(rec_id: int, request: Request, skip_ids: str = ""):
    profile_id = get_profile_id(request)
    rec = db.get_recommendation(rec_id, profile_id)
    stats = db.get_stats(profile_id)
    remaining = stats["pending"] + stats["unrated_recs"]
    return templates.TemplateResponse("partials/review_card.html", {
        "request": request, "rec": rec,
        "skip_ids": skip_ids, "rate_mode": True, "remaining": remaining,
    })


@app.get("/review/{rec_id}/show")
def review_show(rec_id: int, request: Request, skip_ids: str = ""):
    """Re-render a card in triage mode (used by Cancel from rate-confirm)."""
    profile_id = get_profile_id(request)
    rec = db.get_recommendation(rec_id, profile_id)
    stats = db.get_stats(profile_id)
    remaining = stats["pending"] + stats["unrated_recs"]
    return templates.TemplateResponse("partials/review_card.html", {
        "request": request, "rec": rec,
        "skip_ids": skip_ids, "rate_mode": False, "remaining": remaining,
    })


@app.post("/review/{rec_id}/rate")
def review_rate(rec_id: int, request: Request,
                rating: int = Form(...), skip_ids: str = Form("")):
    profile_id = get_profile_id(request)
    db.set_rec_status(rec_id, "read", profile_id)
    db.set_rec_rating(rec_id, rating, profile_id)
    db.remove_from_queue(rec_id, profile_id)
    return _review_card(request, skip_ids)


@app.get("/review/skip/{rec_id}")
def review_skip(rec_id: int, request: Request, skip_ids: str = ""):
    existing = _parse_skip_ids(skip_ids)
    if rec_id not in existing:
        existing.append(rec_id)
    return _review_card(request, ",".join(str(x) for x in existing))


# ---------------------------------------------------------------------------
# Rating queue
# ---------------------------------------------------------------------------

@app.get("/rate", response_class=HTMLResponse)
def rating_queue_page(request: Request):
    profile_id = get_profile_id(request)
    unrated_recs = db.get_unrated_recs(profile_id)
    unrated_hc   = db.get_unrated_hc_books(200)
    return _tmpl(request, "rating_queue.html",
                 unrated_recs=unrated_recs, unrated_hc=unrated_hc)


@app.post("/hc/{book_id}/rate")
def rate_hc(book_id: int, request: Request, rating: int = Form(...)):
    db.rate_hc_book(book_id, rating if rating > 0 else None)
    if request.headers.get("HX-Request"):
        stars = "★" * rating + "☆" * (5 - rating) if rating > 0 else ""
        label = stars if stars else "skipped"
        return HTMLResponse(f'<div id="hc-{book_id}" class="rate-done">{label}</div>')
    return RedirectResponse("/rate", status_code=303)


# ---------------------------------------------------------------------------
# Recommendations — in-app generation
# ---------------------------------------------------------------------------

@app.get("/recs/refresh", response_class=HTMLResponse)
def recs_refresh_page(request: Request):
    profile_id = get_profile_id(request)
    return _tmpl(request, "recs_refresh.html",
                 profile_id=profile_id,
                 api_key_configured=generator.api_key_configured(),
                 gen_running=_gen_running,
                 gen_last=_gen_last)


@app.post("/recs/generate")
def recs_generate(request: Request, count: int = Form(10)):
    global _gen_running
    profile_id = get_profile_id(request)
    if not generator.api_key_configured():
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                "partials/gen_status.html",
                {"request": request, "gen_running": False,
                 "gen_last": {"added": 0, "error": "ANTHROPIC_API_KEY is not set in .env", "finished_at": None}})
        return RedirectResponse("/recs/refresh", status_code=303)

    if not _gen_lock.acquire(blocking=False):
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                "partials/gen_status.html",
                {"request": request, "gen_running": True, "gen_last": _gen_last})
        return RedirectResponse("/recs/refresh", status_code=303)

    t = threading.Thread(target=_run_gen, args=(profile_id, count), daemon=True)
    t.start()

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/gen_status.html",
            {"request": request, "gen_running": True, "gen_last": _gen_last})
    return RedirectResponse("/recs/refresh", status_code=303)


@app.get("/recs/generate/status")
def recs_generate_status(request: Request):
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "partials/gen_status.html",
            {"request": request, "gen_running": _gen_running, "gen_last": _gen_last})
    return JSONResponse({"running": _gen_running, "last": _gen_last})


@app.get("/api/context")
def api_context(request: Request, profile_id: int = None):
    pid = profile_id if profile_id is not None else get_profile_id(request)
    return JSONResponse(db.get_rec_context(pid))


@app.post("/api/recs/import")
async def api_import_recs(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    profile_id = int(data.get("profile_id", 1))
    recs = data.get("recs", [])

    added = 0
    imported: list[tuple[int, str, str]] = []  # (rec_id, title, author)
    for rec in recs:
        tags_list = rec.get("tags") or []
        tags = ", ".join(tags_list) if isinstance(tags_list, list) else tags_list or None
        rec_id = db.upsert_recommendation(
            rec["title"], rec.get("author"), rec.get("series"),
            rec.get("type", "Book"), rec.get("audiobook_available", "Unknown"),
            rec.get("reason"), source="claude-cli", tags=tags
        )
        with db.db() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO rec_interactions (profile_id, rec_id, user_status)
                VALUES (?, ?, 'pending')
            """, (profile_id, rec_id))
        imported.append((rec_id, rec.get("title", ""), rec.get("author", "")))
        added += 1

    background_tasks.add_task(_fetch_missing_covers, imported)
    return JSONResponse({"added": added, "profile_id": profile_id})


async def _fetch_missing_covers(recs: list[tuple[int, str, str]]):
    """Fetch cover art from Open Library for recs that have no HC-matched cover."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        for rec_id, title, author in recs:
            try:
                params = {"title": title, "limit": 1, "fields": "cover_i"}
                if author:
                    params["author"] = author
                resp = await client.get(
                    "https://openlibrary.org/search.json", params=params
                )
                docs = resp.json().get("docs", [])
                if docs and docs[0].get("cover_i"):
                    cover_url = f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-M.jpg"
                    db.update_rec_cover(rec_id, cover_url)
            except Exception:
                pass  # silently skip — covers are best-effort


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@app.post("/sync")
def trigger_sync(background_tasks: BackgroundTasks, request: Request):
    global _sync_running
    if not _sync_lock.acquire(blocking=False):
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="sync-status running">Already running…</span>')
        return RedirectResponse("/", status_code=303)
    _sync_running = True  # set eagerly so panel sees it immediately
    _sync_lock.release()  # _run_sync will re-acquire
    background_tasks.add_task(_run_sync)
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="sync-status running">Sync started…</span>')
    return RedirectResponse("/", status_code=303)


@app.get("/sync/status")
def sync_status(request: Request):
    last = db.get_last_sync()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/sync_status.html",
                                          {"request": request, "last_sync": last,
                                           "running": _sync_running})
    return {"running": _sync_running, "last_sync": dict(last) if last else None}


@app.get("/sync/log-panel")
def sync_log_panel(request: Request):
    entries = list(reversed(db.get_app_log(limit=50)))
    return templates.TemplateResponse("partials/sync_log_panel.html", {
        "request": request,
        "entries": entries,
        "running": _sync_running,
    })


@app.get("/sync/log-entries")
def sync_log_entries():
    """JSON endpoint for JS polling — avoids HTMX XHR browser spinner."""
    entries = list(reversed(db.get_app_log(limit=50)))
    return {
        "running": _sync_running,
        "entries": [
            {
                "time": e["created_at"][11:19],
                "level": e["level"],
                "component": e["component"],
                "message": e["message"],
                "detail": e["detail"],
            }
            for e in entries
        ],
    }


# ---------------------------------------------------------------------------
# ABS cover proxy + book detail modal
# ---------------------------------------------------------------------------

@app.get("/abs/cover/{item_id}")
def abs_cover(item_id: str):
    abs_url = os.environ.get("ABS_URL", "")
    abs_token = os.environ.get("ABS_TOKEN", "")
    if not abs_url or not abs_token:
        return Response(status_code=404)
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(
                f"{abs_url}/api/items/{item_id}/cover",
                headers={"Authorization": f"Bearer {abs_token}"},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return Response(resp.content,
                                media_type=resp.headers.get("content-type", "image/jpeg"),
                                headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        pass
    return Response(status_code=404)


@app.get("/rec/{rec_id}/detail", response_class=HTMLResponse)
def rec_detail(rec_id: int, request: Request):
    profile_id = get_profile_id(request)
    rec = db.get_rec_detail(rec_id, profile_id)
    if not rec:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse("partials/book_detail_modal.html",
                                      {"request": request, "rec": rec})


# ---------------------------------------------------------------------------
# Settings / log
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    app_log = db.get_app_log(200)
    sync_history = db.get_sync_history(20)
    return _tmpl(request, "settings.html", app_log=app_log, sync_history=sync_history)


@app.post("/settings/log/clear")
def clear_log(request: Request):
    db.clear_app_log()
    if request.headers.get("HX-Request"):
        return HTMLResponse('<p class="empty">Log cleared.</p>')
    return RedirectResponse("/settings", status_code=303)
