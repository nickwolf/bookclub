# Bookclub

A personal book recommendation app — syncs reading history from Hardcover and audiobook library from Audiobookshelf, then uses the Anthropic API to generate tailored recommendations per reader profile.

## Key Docs

All reference material lives in `docs/`:

| File | Contents |
|------|----------|
| `docs/architecture.md` | DB schema, request flow, key patterns (`_REC_COLS`, profile-aware queries, HTMX swap pattern) |
| `docs/ops-runbook.md` | Dev commands, env vars, troubleshooting, known gotchas |
| `docs/design-system.md` | CSS tokens, typography, theme switching, layout decisions |
| `docs/product-requirements.md` | Feature spec and intended behavior |
| `docs/user-guide.md` | End-user documentation |

**Read `docs/ops-runbook.md` → "Known Gotchas" before touching sync, HTMX, or the ABS integration.**

## Dev Commands

```bash
# After any code change
docker compose up -d --build

# Logs
docker logs -f bookclub

# DB shell
docker exec -it bookclub sqlite3 /data/bookclub.db
```

## Critical Conventions

- `recommendations` is a **global catalog** — not per-profile. Per-profile state lives in `rec_interactions` and `queue` only.
- All HTMX card actions return `rec_card.html` via the `_card()` helper in `main.py` — keep that pattern.
- Timestamps: always use `_now()` from `db.py`, never `CURRENT_TIMESTAMP` (which is UTC regardless of TZ).
- `docker compose up` fails from WSL2 when compose files have Windows-style paths. Use `docker stop/start` or `docker update` for config changes instead of recreating containers.
