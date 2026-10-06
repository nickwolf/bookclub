"""Title normalisation and duplicate filtering shared by sync and generation."""

import difflib
import re
import unicodedata

FUZZY_CUTOFF = 0.92
_TRAILING_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")
_GENERIC_PART = re.compile(
    r"^((book|volume|vol|part)\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten)?"
    r"|(graphic )?novel|novella|memoir|thriller|story|stories|short stories|mystery|fantasy"
    r"|romance|science fiction|sci fi)$")


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
        parts = [_norm(p) for p in title.split(":")]
        generic = any(_GENERIC_PART.match(n) for n in parts)
        for n in parts:
            if n and not _GENERIC_PART.match(n) and (generic or len(n.split()) >= 2):
                keys.add(n)
    return keys


_PLACEHOLDER_AUTHORS = {"unknown", "unknown author", "anonymous", "n/a", "na", "various"}


def author_surnames(author: str | None) -> set[str]:
    """Lowercased ascii last words of each name in a comma/and/ampersand separated author list."""
    out = set()
    for name in re.split(r",|&|\band\b", author or ""):
        if name.strip().lower() in _PLACEHOLDER_AUTHORS:
            continue
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
        words = re.sub(r"[^\w\s]", " ", name).split()
        if words:
            out.add(words[-1])
    return out


def add_blocked(blocked: dict[str, set[str]], title: str, author: str | None = None):
    """Record a title's keys in a key -> author-surnames map."""
    surnames = author_surnames(author)
    for key in title_keys(title):
        blocked.setdefault(key, set()).update(surnames)


def _is_blocked(keys: set[str], surnames: set[str], blocked: dict[str, set[str]]) -> bool:
    if keys & blocked.keys():
        return True
    for key in keys:
        for b, b_surnames in blocked.items():
            m = difflib.SequenceMatcher(None, key, b)
            if m.real_quick_ratio() >= FUZZY_CUTOFF and m.quick_ratio() >= FUZZY_CUTOFF \
                    and m.ratio() >= FUZZY_CUTOFF \
                    and (not surnames or not b_surnames or surnames & b_surnames):
                return True
    return False


def filter_duplicates(recs: list[dict], blocked_keys: dict[str, set[str]],
                      blocked_series: set[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """Split recs into (kept, skipped); kept recs also block later ones in the batch."""
    blocked = {k: set(v) for k, v in blocked_keys.items()}
    kept, skipped = [], []
    for rec in recs:
        title = (rec.get("title") or "").strip()
        keys = title_keys(title)
        if not keys:
            skipped.append(rec)
            continue
        dup = _is_blocked(keys, author_surnames(rec.get("author")), blocked)
        if not dup:
            names = {_norm(rec.get("series") or "")}
            if rec.get("type") == "Series":
                names.add(_norm(title))
            dup = bool((names - {""}) & blocked_series)
        if dup:
            skipped.append(rec)
            continue
        add_blocked(blocked, title, rec.get("author"))
        kept.append(rec)
    return kept, skipped
