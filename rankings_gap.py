"""List the Digg Tech X ranking accounts you do NOT already follow on X.

Read-only: this never posts, follows, likes, or reposts. It reuses the cached
twikit browser session (cookies.json, same as digest.py), pages through your
full following list, fetches the Digg rankings, and writes the set-difference to
digests/digg-x-rankings-not-followed-<YYYY-MM-DD>.md.

Run it: python rankings_gap.py
"""

import asyncio
import html as ihtml
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from twikit import Client

# Reuse digest.py's auth (and, via that import, the twikit_patch that tolerates
# trimmed X user payloads). authenticate() loads the same cookies.json session.
from digest import ROOT, DIGEST_DIR, authenticate

RANKINGS_URL = "https://digg.com/tech/x/rankings"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FOLLOWING_PAGE = 200       # accounts per page when paging your following list
MAX_FOLLOWING_PAGES = 200  # hard stop so a paging bug can't loop forever

# Each ranking card server-renders a rank span, an <h2> display name, and an
# <a href="https://x.com/<handle>"> link. We key off those stable markers rather
# than guessing: the rank span anchors the card, and the next h2 + x.com anchor
# inside it give the name and handle.
RANK_RE = re.compile(r'data-slot="ranked-avatar-rank"[^>]*>(\d+)</span>')
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)
HANDLE_RE = re.compile(r'href="https://(?:x|twitter)\.com/([A-Za-z0-9_]+)"')


class Account:
    def __init__(self, rank: int, name: str, handle: str):
        self.rank = rank
        self.name = name
        self.handle = handle


def fetch_rankings() -> list[Account]:
    """Pull rank, display name, and @handle for every ranked account from the
    server-rendered Digg page. Stops the whole run if the structured markers
    can't be found -- never returns guessed or partial-looking data silently."""
    req = urllib.request.Request(RANKINGS_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            doc = resp.read().decode("utf-8", "replace")
    except Exception as err:  # noqa: BLE001
        sys.exit(f"Could not fetch {RANKINGS_URL}: {err}")

    accounts: list[Account] = []
    seen_handles: set[str] = set()
    for match in RANK_RE.finditer(doc):
        rank = int(match.group(1))
        card = doc[match.end():match.end() + 1500]
        h2 = H2_RE.search(card)
        handle = HANDLE_RE.search(card)
        if not h2 or not handle:
            continue
        name = ihtml.unescape(re.sub(r"<[^>]+>", "", h2.group(1))).strip()
        key = handle.group(1).lower()
        if key in seen_handles:
            continue
        seen_handles.add(key)
        accounts.append(Account(rank, name, handle.group(1)))

    if not accounts:
        sys.exit(
            "Could not extract any ranked accounts from the Digg page -- its "
            "structure may have changed. Refusing to guess; wrote nothing.")

    accounts.sort(key=lambda a: a.rank)
    ranks = [a.rank for a in accounts]
    if ranks != list(range(ranks[0], ranks[0] + len(ranks))):
        sys.exit(
            f"Ranks from the Digg page are not a contiguous sequence "
            f"(got {ranks[0]}..{ranks[-1]} across {len(ranks)} entries). "
            "Refusing to emit a possibly-incomplete list; wrote nothing.")
    return accounts


async def fetch_following(client: Client) -> set[str]:
    """Page through the authenticated user's complete following list, returning
    lowercased @screen_names."""
    me = await client.user()
    handles: set[str] = set()
    page = await client.get_user_following(me.id, count=FOLLOWING_PAGE)
    pages = 0
    while page and pages < MAX_FOLLOWING_PAGES:
        pages += 1
        for user in page:
            screen_name = getattr(user, "screen_name", None)
            if screen_name:
                handles.add(screen_name.lower())
        nxt = await page.next()
        if not nxt or len(nxt) == 0:
            break
        page = nxt
    print(f"  collected {len(handles)} followed accounts across {pages} page(s)")
    return handles


def write_output(ranked: list[Account], following: set[str]) -> Path:
    not_followed = [a for a in ranked if a.handle.lower() not in following]
    already = len(ranked) - len(not_followed)
    today = datetime.now().strftime("%Y-%m-%d")

    lines = ["# Digg Tech X rankings -- accounts you don't follow", ""]
    lines.append(
        f"{len(ranked)} ranked / {already} already followed / "
        f"{len(not_followed)} not followed.")
    lines.append("")
    lines.append(f"Source: {RANKINGS_URL} (fetched {today})")
    lines.append("")
    for i, a in enumerate(not_followed, 1):
        lines.append(f"{i}. {a.name} — [@{a.handle}](https://x.com/{a.handle})")
    lines.append("")

    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"digg-x-rankings-not-followed-{today}.md"
    path.write_text("\n".join(lines))
    return path


async def run() -> Path:
    print(f"Fetching Digg rankings from {RANKINGS_URL} ...")
    ranked = fetch_rankings()
    print(f"  parsed {len(ranked)} ranked accounts (ranks "
          f"{ranked[0].rank}-{ranked[-1].rank})")

    client = Client("en-US")
    authenticate(client)
    print("Paging through your following list ...")
    following = await fetch_following(client)

    return write_output(ranked, following)


def main() -> None:
    load_dotenv(ROOT / ".env")
    path = asyncio.run(run())
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
