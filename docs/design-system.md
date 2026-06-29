# Bookclub — Design System

Implemented from a design handoff (2026-04-25). Light is the default theme; dark via toggle.

---

## Typography

| Role | Font |
|------|------|
| Book titles, headings | `Georgia, "Times New Roman", serif` → `var(--f-title)` |
| All UI chrome (nav, buttons, labels, body) | `'DM Sans', system-ui, -apple-system, sans-serif` → `var(--f-ui)` |

Base: 14px body, 1.5 line-height.

---

## Color Tokens

### Light theme (`:root` default)

| Variable | Value | Purpose |
|----------|-------|---------|
| `--bg` | `#faf6f0` | Page background |
| `--surface` | `#ffffff` | Cards, nav |
| `--surface2` | `#f2ece2` | Subtle inset areas |
| `--surface3` | `#e8dfd2` | Deeper inset |
| `--border` | `#ddd4c4` | Dividers, card borders |
| `--text` | `#1c1410` | Primary text |
| `--text-muted` | `#7a6b58` | Secondary text |
| `--text-faint` | `#a89880` | Placeholder, metadata |
| `--accent` | `#b06820` | Amber — primary interactive |
| `--accent-dim` | `rgba(176,104,32,0.1)` | Hover backgrounds |
| `--accent-border` | `rgba(176,104,32,0.28)` | Accent-tinted borders |
| `--green` | `#2e7a4e` | "In library" / positive badges |
| `--green-dim` | `rgba(46,122,78,0.1)` | Green badge bg |
| `--green-border` | `rgba(46,122,78,0.28)` | Green badge border |
| `--teal` | `#2a7a6e` | Secondary accent |
| `--teal-dim` | `rgba(42,122,110,0.1)` | Teal badge bg |
| `--teal-border` | `rgba(42,122,110,0.28)` | Teal badge border |
| `--yellow` | `#9a6e10` | Warning / partial |
| `--red` | `#9a3838` | Error / destructive |
| `--radius` | `8px` | Default border-radius |
| `--shadow` | `0 4px 28px rgba(0,0,0,0.1)` | Modal / overlay shadow |
| `--shadow-card` | `0 2px 8px rgba(0,0,0,0.07)` | Card shadow |

### Dark theme (`[data-theme="dark"]` on `<html>`)

| Variable | Value |
|----------|-------|
| `--bg` | `#16120e` |
| `--surface` | `#1e1a14` |
| `--surface2` | `#28221a` |
| `--surface3` | `#342b20` |
| `--border` | `#3c3226` |
| `--text` | `#ede8dc` |
| `--text-muted` | `#9a8f7e` |
| `--text-faint` | `#625648` |
| `--accent` | `#d4883a` |
| `--green` | `#6db88a` |
| `--teal` | `#4a9e8e` |
| `--yellow` | `#e0b855` |
| `--red` | `#d97575` |

---

## Theme Switching

- Toggle button `◑` in the nav bar
- Writes `localStorage` key `bc_theme` (`"dark"` or `"light"`)
- Initialized via inline `<script>` in `<head>` (before CSS paint) to prevent flash
- Sets `data-theme="dark"` on `<html>` when dark

---

## Layout Decisions

| View | Layout |
|------|--------|
| Recommendations grid | 3 fixed columns |
| History grid | 6 fixed columns |
| Rate-from-Hardcover grid | 6 fixed columns |
| Queue | max-width 820px |
| Review page | max-width 580px, centered |

---

## Rec Card — Cover Slot

- Cover column: 88px wide, `padding: 14px 0 14px 14px`
- Image: 72px wide, natural height
- "Audio" text label appears below the cover when `audiobook_available == "Yes"`

---

## Tag Expand Pattern

- Max 2 tags shown by default
- "+N more" button reveals remaining tags
- Implemented via JS event delegation in `base.html` (not per-card JS)
