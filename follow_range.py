"""Follow a chosen index range from the Digg X rankings not-followed list.

This is a WRITE tool: it follows accounts on X, but ONLY when run without
--dry-run. Writing/importing the script touches nothing; only running the live
path does. It reads the generated list
(digests/digg-x-rankings-not-followed-<date>.md), reuses the cached twikit
browser session (cookies.json, same as rankings_gap.py / digest.py), and
follows entries N..M (1-based, inclusive) one at a time with a jittered pause
between real follows so a single chunk stays under X's rolling-window soft cap.

Work through the list in safe chunks, e.g.:

    python follow_range.py 1 15      # then wait, then ...
    python follow_range.py 16 30

Preview a chunk without touching X:

    python follow_range.py 1 15 --dry-run

On a rate-limit / X error it stops immediately and prints the index to resume
from, so you can re-run later starting at that number.
"""

import argparse
import asyncio
import random
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from twikit import Client

# Reuse digest.py's auth (and, via that import, the twikit_patch that tolerates
# trimmed X user payloads). authenticate() loads the same cookies.json session.
from digest import ROOT, DIGEST_DIR, authenticate

LIST_GLOB = "digg-x-rankings-not-followed-*.md"
# Numbered lines look like: "12. David Duvenaud — [@DavidDuvenaud](https://x.com/DavidDuvenaud)"
# We need the leading index, the display name, and the @handle.
ENTRY_RE = re.compile(
    r"^\s*(\d+)\.\s+(.*?)\s+—\s+\[@([A-Za-z0-9_]+)\]\([^)]*\)\s*$")

DEFAULT_DELAY_MIN = 30
DEFAULT_DELAY_MAX = 60


class Entry:
    def __init__(self, index: int, name: str, handle: str):
        self.index = index
        self.name = name
        self.handle = handle


def latest_list_file() -> Path:
    """Most recent not-followed list in DIGEST_DIR, by name (dates sort)."""
    candidates = sorted(DIGEST_DIR.glob(LIST_GLOB))
    if not candidates:
        sys.exit(
            f"No list file found matching {DIGEST_DIR}/{LIST_GLOB}. "
            "Run rankings_gap.py first, or pass --file.")
    return candidates[-1]


def parse_entries(path: Path) -> list[Entry]:
    """Parse the numbered "N. Name — [@handle](url)" lines into ordered entries.

    The file numbers are 1..len and contiguous by construction; we key off the
    parsed line index so the numbers shown to the user match the file exactly."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        sys.exit(f"List file not found: {path}")
    except OSError as err:  # noqa: BLE001
        sys.exit(f"Could not read list file {path}: {err}")

    entries: list[Entry] = []
    for line in text.splitlines():
        m = ENTRY_RE.match(line)
        if m:
            entries.append(Entry(int(m.group(1)), m.group(2).strip(), m.group(3)))

    if not entries:
        sys.exit(
            f"No entries parsed from {path}. Expected numbered lines like "
            "'1. Name — [@handle](https://x.com/handle)'.")
    return entries


def select_range(entries: list[Entry], n: int, m: int) -> list[Entry]:
    """Validate the 1-based inclusive range N..M against the parsed entries."""
    total = len(entries)
    if n < 1 or m < 1:
        sys.exit(f"N and M must be >= 1 (got N={n}, M={m}).")
    if n > m:
        sys.exit(f"Inverted range: N ({n}) must be <= M ({m}).")
    if n > total:
        sys.exit(
            f"N ({n}) is past the end of the list ({total} entries). "
            "Nothing to do.")
    if m > total:
        print(f"Note: M ({m}) exceeds the list length ({total}); "
              f"clamping to {total}.")
        m = total
    return [e for e in entries if n <= e.index <= m]


async def resolve(client: Client, handle: str):
    """@handle -> twikit user (or raise)."""
    return await client.get_user_by_screen_name(handle)


def jittered_sleep(delay_min: int, delay_max: int) -> None:
    secs = random.uniform(delay_min, delay_max)
    print(f"  pausing {secs:.0f}s before next follow ...")
    time.sleep(secs)


async def run(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else latest_list_file()
    entries = parse_entries(path)
    selected = select_range(entries, args.n, args.m)

    print(f"List: {path}")
    print(f"Parsed {len(entries)} entries; selected {len(selected)} in range "
          f"{args.n}..{args.m}.")
    mode = "DRY RUN (no follows, no delays)" if args.dry_run else "LIVE"
    print(f"Mode: {mode}\n")

    if args.dry_run:
        for e in selected:
            print(f"  {e.index}. would follow @{e.handle} ({e.name})")
        print(f"\nWould attempt {len(selected)} account(s). No X calls made.")
        return 0

    client = Client("en-US")
    authenticate(client)

    followed = skipped = failed = 0
    for pos, e in enumerate(selected):
        try:
            user = await resolve(client, e.handle)
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"  {e.index}. ERROR resolving @{e.handle}: {err}")
            print(f"\nStopped on an X error. Re-run with N={e.index} "
                  f"(end M={args.m}) to resume.")
            print(f"followed {followed} / skipped {skipped} / failed {failed}")
            return 1

        if getattr(user, "following", None) is True:
            skipped += 1
            print(f"  {e.index}. skip (already following) @{e.handle}")
            continue

        try:
            await client.follow_user(user.id)
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"  {e.index}. ERROR following @{e.handle}: {err}")
            print(f"\nStopped on an X error. Re-run with N={e.index} "
                  f"(end M={args.m}) to resume.")
            print(f"followed {followed} / skipped {skipped} / failed {failed}")
            return 1

        followed += 1
        print(f"  {e.index}. followed @{e.handle}")

        # Jittered pause only between *actual* follows -- not after a skip and
        # not after the final entry in the range.
        if pos < len(selected) - 1:
            jittered_sleep(args.delay_min, args.delay_max)

    print(f"\nDone. followed {followed} / skipped {skipped} / failed {failed}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Follow entries N..M (1-based, inclusive) from the Digg X "
                    "rankings not-followed list.")
    p.add_argument("n", type=int, metavar="N", help="start index (1-based)")
    p.add_argument("m", type=int, metavar="M", help="end index (1-based, inclusive)")
    p.add_argument("--file", help="path to the list .md (default: latest in digests/)")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve + print who would be followed; no X calls, no delays")
    p.add_argument("--delay-min", type=int, default=DEFAULT_DELAY_MIN,
                   help=f"min seconds between real follows (default {DEFAULT_DELAY_MIN})")
    p.add_argument("--delay-max", type=int, default=DEFAULT_DELAY_MAX,
                   help=f"max seconds between real follows (default {DEFAULT_DELAY_MAX})")
    args = p.parse_args()
    if args.delay_min < 0 or args.delay_max < 0:
        p.error("--delay-min and --delay-max must be non-negative")
    if args.delay_min > args.delay_max:
        p.error("--delay-min must be <= --delay-max")
    return args


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
