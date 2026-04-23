# Bookclub — User Guide

Bookclub is a personal reading companion that pulls your reading history from [Hardcover](https://hardcover.app), cross-references it with your [Audiobookshelf](https://www.audiobookshelf.org) library, and uses Claude to generate book recommendations tailored to your taste.

---

## Getting Started

The app runs at **http://localhost:8585** (or whatever port is configured in `.env`).

On first load the app is pre-seeded with a starter list of ~30 recommendations. Run a **Sync** to pull in your real Hardcover data, then use **↺ Recs** to generate personalised AI recommendations.

---

## Navigation

| Page | What it's for |
|------|---------------|
| **Recommendations** | Browse, queue, pass on, or mark books read |
| **Queue** | Your ordered reading list — drag to reorder |
| **Rate** | Catch up on unrated books (Hardcover history + read recs) |
| **History** | Your full Hardcover read shelf, searchable and paginated |
| Profile chip (top-right) | Switch between profiles / manage preferences |

---

## Syncing

The **⟳ Sync** button (top-right) triggers a background sync that:

1. Pulls all your books from Hardcover (want-to-read, reading, read, DNF) — paginated, handles large libraries
2. Reads your Audiobookshelf SQLite database directly for library contents and listen progress
3. Cross-references every recommendation against both sources using fuzzy title matching
4. Links matched recs to their Hardcover entry (enabling cover art display)

The nav shows the last sync time once it completes. The status indicator polls automatically while a sync is running, then stops — it does not poll when idle.

**Sync is non-destructive** — it only upserts data, never deletes recommendations or ratings you've entered.

---

## Recommendations Page

### Filter Tabs

| Tab | Shows |
|-----|-------|
| **All** | Every recommendation |
| **Pending** | Unactioned recs — your default working view |
| **Queued** | Recs you've added to your reading queue |
| **In Library** | Pending recs you already own in Audiobookshelf — highest-conversion view |
| **Pass** | Recs you've declined |
| **Read** | Recs you've marked read |

### Card Actions

**Pending recs:**
- **+ Queue** — adds to your queue
- **Pass** — moves to the Pass tab; add a note explaining why for better future recommendations
- **Already Read** — marks as read without going through the queue

**Queued recs:**
- Shows your position in the queue (`📋 Queued #2`)
- **Mark Read** — removes from queue, moves to Read
- **Remove** — removes from queue, moves back to Pass

**Read recs:**
- Shows star rating if rated; otherwise presents a 1–5 star quick-rate form

**Passed recs:**
- **Undo** — returns to Pending

### Notes

Notes appear on any non-pending card. They are fed back to Claude as context when you next generate recommendations — so a note like "too slow for me" or "loved this, want more like it" directly influences future suggestions.

### Cover Art

Cards show cover art when available:
- **Direct match**: the rec was fuzzy-matched to a book in your Hardcover library during sync
- **Series fallback**: if no direct match, the cover of book 1 of the same series is used
- **Placeholder**: 📖 icon when no cover is found

Run a sync after adding new recs to improve cover match rates.

---

## Queue Page

Your ordered to-read list.

- **Drag** the ≡ handle to reorder
- **↑ / ↓** buttons for keyboard reordering
- **Remove** removes from queue (moves back to Pending)
- **Mark Read** completes the book and prompts for a rating

Ratings on queue cards are shown inline once set.

---

## Rating Queue (`/rate`)

A focused view for catching up on unrated books. Two sections:

**Unrated recommendations** — recs you've marked Read but haven't rated yet. Rating these improves AI context.

**Unrated Hardcover books** — books from your Hardcover Read shelf without a star rating. These feed into the `top_rated_books` context used for AI recommendations.

The badge count in the nav combines both totals.

---

## History Page

Your full Hardcover read shelf. Filters:
- **Search** by title or author
- **Rating filter**: 5★ only, 4★+, 3★+

Paginated at 50 books per page. The subtitle shows how many books match the current filter.

---

## Generating Recommendations (↺ Recs)

Click **↺ Recs** in the nav to open the generation page. The app connects to the Anthropic API and generates recommendations directly — no external script needed.

### What context is sent to Claude

| Signal | Source |
|--------|--------|
| Stated preferences | Your profile's preferences text |
| Top-rated books (4–5★) | Hardcover read shelf, up to 60 books |
| Currently reading | Hardcover in-progress shelf |
| DNF books | Hardcover did-not-finish shelf |
| Passed recs with notes | Your Pass notes from this app |
| Read recs with ratings/notes | Recs you've marked read and rated/noted |
| Existing recs (deduplication) | All current recommendations — Claude is told not to repeat these |

Each generated recommendation includes a **confidence score** (0–100) showing how well Claude thinks the book fits your taste.

### Legacy: host-side script

`scripts/refresh_recs.py` is an alternative that calls the Claude Code CLI from outside Docker. Useful if `ANTHROPIC_API_KEY` isn't configured in the container, or for scripting / cron jobs.

```bash
python3 /path/to/bookclub/scripts/refresh_recs.py --profile 1 --count 10
```

| Flag | Default | Description |
|------|---------|-------------|
| `--profile ID` | `1` | Which profile to generate for |
| `--count N` | `10` | How many new recs to generate |
| `--dry-run` | off | Print the prompt without calling Claude |
| `--base-url URL` | `http://localhost:8585` | App URL if using a non-default port |

---

## Profiles

Each profile has its own:
- Queue
- Rec statuses (pending / queued / pass / read)
- Ratings and notes
- AI recommendation preferences

**Recommendations themselves are shared** — the catalog is one pool, but each profile tracks its own relationship with each rec.

### Switching profiles

Click the profile chip in the top-right nav to switch, or go to **Manage profiles…** to add a new profile or update preferences.

### Preferences

Each profile has a free-text preferences field that is injected at the top of every AI prompt — highest-signal input for recommendation quality. Examples:

> I prefer long series over standalones, love fast-paced progression fantasy, avoid horror and heavy grimdark.

> I like cozy fantasy and literary fiction. Not interested in military sci-fi or epic secondary-world fantasy with large casts.

---

## Tips for Better Recommendations

1. **Rate your Hardcover books** — the rating queue makes this fast. The AI uses 4–5★ books as its main taste signal.
2. **Write Pass notes** — "too slow" or "not interested in [trope]" directly shapes future prompts.
3. **Set preferences** — even a sentence helps the AI avoid whole genres you dislike.
4. **Sync before generating** — ensures Claude sees your latest reading history and DNF books.
5. **Use `--dry-run`** — inspect the full prompt before burning a Claude call. Useful for debugging why recs feel off.
6. **Mark recs Read and rate them** — closes the feedback loop so Claude knows what it got right.
