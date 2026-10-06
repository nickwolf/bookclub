"""Title normalisation and duplicate filtering shared by sync and generation."""

import difflib
import re
import unicodedata

FUZZY_CUTOFF = 0.92
_TRAILING_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")
_GENERIC_PART = re.compile(r"^(book|volume|vol|part)\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten)?$")


def _norm(title: str) -> str:
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    title = title.lower()
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\b(the|a|an)\b", "", title)
    return re.sub(r"\s+", " ", title).strip()


def title_keys(title: str) -> set[str]:
    """Normalized full title plus the useful halves of a 'Series: Subtitle' title."""
    title = _TRAILING_PAREN.sub("", title or "").strip()
    keys = set()
    full = _norm(title)
    if full:
        keys.add(full)
    if ":" in title:
        for part in title.split(":"):
            n = _norm(part)
            if len(n.split()) >= 2 and not _GENERIC_PART.match(n):
                keys.add(n)
    return keys


def _is_blocked(keys: set[str], blocked: set[str]) -> bool:
    if keys & blocked:
        return True
    for key in keys:
        for b in blocked:
            m = difflib.SequenceMatcher(None, key, b)
            if m.real_quick_ratio() >= FUZZY_CUTOFF and m.quick_ratio() >= FUZZY_CUTOFF \
                    and m.ratio() >= FUZZY_CUTOFF:
                return True
    return False


def filter_duplicates(recs: list[dict], blocked_keys: set[str],
                      blocked_series: set[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """Split recs into (kept, skipped); kept recs also block later ones in the batch."""
    blocked = set(blocked_keys)
    kept, skipped = [], []
    for rec in recs:
        title = (rec.get("title") or "").strip()
        keys = title_keys(title)
        if not keys:
            skipped.append(rec)
            continue
        dup = _is_blocked(keys, blocked)
        if not dup and rec.get("type") == "Series":
            names = {_norm(rec.get("series") or ""), _norm(title)} - {""}
            dup = bool(names & blocked_series)
        if dup:
            skipped.append(rec)
            continue
        blocked |= keys
        kept.append(rec)
    return kept, skipped
