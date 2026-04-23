#!/usr/bin/env python3
"""
Refresh bookclub recommendations using the Claude Code CLI (legacy host-side script).
The in-app generation via Anthropic API is the primary path; use this for scripting
or when ANTHROPIC_API_KEY is not configured in the container.

Run on the host — the bookclub app must be running.

Usage:
  python3 /path/to/bookclub/scripts/refresh_recs.py [--profile ID] [--count N] [--dry-run]
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
import urllib.error


def get_context(base_url: str, profile_id: int) -> dict:
    url = f"{base_url}/api/context?profile_id={profile_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"Error: could not reach the app at {base_url} — is it running?", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)


def build_prompt(ctx: dict, count: int) -> str:
    profile_name = ctx["profile_name"]

    sections = [f"You are recommending books for {profile_name}."]

    # Stated preferences (highest signal — goes first)
    if ctx.get("preferences"):
        sections.append(f"Their stated reading preferences:\n  {ctx['preferences']}")

    # Want to read — taste signal AND deduplication
    if ctx.get("want_to_read"):
        wtr_lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'}"
            for b in ctx["want_to_read"][:100]
        )
        sections.append(
            f"Books already on their Want-to-Read list (do NOT recommend these — "
            f"they've already found them, but use as taste signal):\n{wtr_lines}"
        )

    # Currently reading
    if ctx.get("currently_reading"):
        cr_lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'}"
            + (f" (series: {b['series']})" if b.get("series") else "")
            for b in ctx["currently_reading"]
        )
        sections.append(f"Currently reading (do not recommend sequels they'll get to naturally):\n{cr_lines}")

    # Top-rated history
    top_books = ctx["top_rated_books"][:60]
    top_str = "\n".join(
        f"  - {b['title']} by {b.get('author') or 'Unknown'}"
        + (f" (series: {b['series']})" if b.get("series") else "")
        + f" — {b['rating']}★"
        for b in top_books
    )
    sections.append(f"Their highest-rated books from Hardcover (4–5 stars):\n{top_str}")

    # Passed recs with notes
    if ctx.get("passed_with_notes"):
        passed_lines = "\n".join(
            f"  - {r['title']}: {r['user_notes']}"
            for r in ctx["passed_with_notes"]
        )
        sections.append(f"Recommendations they passed on and why:\n{passed_lines}")

    # Read recs with ratings/notes
    if ctx.get("read_recs"):
        read_lines = "\n".join(
            f"  - {r['title']}"
            + (f" ({r['user_rating']}★)" if r.get("user_rating") else "")
            + (f" — {r['user_notes']}" if r.get("user_notes") else "")
            for r in ctx["read_recs"]
        )
        sections.append(f"Recommendations they've already read and rated:\n{read_lines}")

    # DNF — strong negative signal
    if ctx.get("dnf_books"):
        dnf_lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'}"
            for b in ctx["dnf_books"]
        )
        sections.append(
            f"Books they did not finish (avoid recommending similar — "
            f"pay attention to what these have in common):\n{dnf_lines}"
        )

    # Low-rated finished books — strong negative signal (worse than DNF: they finished and still disliked)
    if ctx.get("low_rated_books"):
        low_lines = "\n".join(
            f"  - {b['title']} by {b.get('author') or 'Unknown'} — {b['rating']}★"
            for b in ctx["low_rated_books"]
        )
        sections.append(
            f"Books they finished but rated poorly (1–2 stars) — stronger negative signal than DNF, "
            f"pay close attention to what these have in common:\n{low_lines}"
        )

    # Existing recs — deduplication
    existing_titles = [r["title"] for r in ctx.get("existing_recs", [])]
    if existing_titles:
        sections.append(f"Already recommended — do NOT repeat these:\n  {', '.join(existing_titles)}")

    sections += [
        "",
        f"Generate exactly {count} NEW book or series recommendations they have not read and are not listed above.",
        "Focus on finding logical next-reads and gaps given their taste profile.",
        "",
        "Respond with ONLY a JSON array, no explanation, no markdown fences:",
        '[{"title":"...","author":"...","series":"... or null","type":"Book or Series","audiobook_available":"Yes, No, or Partial","reason":"1-2 sentences why this fits their taste","tags":["genre","subgenre","theme"]}]',
    ]

    return "\n\n".join(sections)


def extract_json(text: str) -> list:
    """Parse JSON from Claude's response, stripping any markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    # Find the JSON array if there's preamble text
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON array found in response")
    text = match.group(0)
    return json.loads(text)


def run_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        print("Error: 'claude' not found in PATH. Is Claude Code installed?", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: Claude timed out after 180 seconds.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"Claude exited with code {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    return result.stdout


def import_recs(base_url: str, profile_id: int, recs: list) -> dict:
    data = json.dumps({"profile_id": profile_id, "recs": recs}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/recs/import",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    parser = argparse.ArgumentParser(
        description="Refresh bookclub recommendations using Claude Code CLI"
    )
    parser.add_argument("--profile", type=int, default=1, metavar="ID",
                        help="Profile ID to generate recommendations for (default: 1)")
    parser.add_argument("--count", type=int, default=10,
                        help="Number of new recommendations to generate (default: 10)")
    parser.add_argument("--base-url", default="http://localhost:8585",
                        help="Bookclub app URL (default: http://localhost:8585)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the prompt without calling Claude or importing")
    args = parser.parse_args()

    print(f"Fetching context for profile {args.profile} from {args.base_url}…")
    ctx = get_context(args.base_url, args.profile)
    print(f"  Profile:               {ctx['profile_name']}")
    print(f"  Preferences set:       {'yes' if ctx.get('preferences') else 'no'}")
    print(f"  Currently reading:     {len(ctx.get('currently_reading', []))}")
    print(f"  Top-rated books:       {len(ctx['top_rated_books'])}")
    print(f"  DNF books:             {len(ctx.get('dnf_books', []))}")
    print(f"  Existing recs:         {len(ctx['existing_recs'])}")
    print(f"  Passed with notes:     {len(ctx['passed_with_notes'])}")

    prompt = build_prompt(ctx, args.count)

    if args.dry_run:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---")
        return

    print(f"\nCalling Claude to generate {args.count} recommendations…")
    response = run_claude(prompt)

    print("Parsing response…")
    try:
        recs = extract_json(response)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Failed to parse Claude's response: {e}", file=sys.stderr)
        print("Raw response:", file=sys.stderr)
        print(response, file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(recs)} recommendations. Importing…")
    result = import_recs(args.base_url, args.profile, recs)
    print(f"Done! {result['added']} new recommendation(s) added for profile {result['profile_id']}.")
    print(f"Open {args.base_url} to see them.")


if __name__ == "__main__":
    main()
