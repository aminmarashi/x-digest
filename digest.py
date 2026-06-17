"""Fetch your X timeline, score it against persona.md with Claude, write a daily reading list.

Run it once a day: python digest.py
Output lands in digests/YYYY-MM-DD.md
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from twikit import Client

import twikit_patch  # noqa: F401  applies the d60/twikit#408 fix on import

ROOT = Path(__file__).parent
COOKIES_FILE = ROOT / "cookies.json"
STATE_FILE = ROOT / "actions.json"
REPOSTS_FILE = ROOT / "reposts.json"
DIGEST_DIR = ROOT / "digests"
PERSONA = (ROOT / "persona.md").read_text()

# How many of your own reposts to fetch (cap, kept small to stay polite to X) and how many
# to feed into the scoring prompt as positive calibration. Plain module constants, NOT env
# knobs: module-level os.getenv runs at import, before load_dotenv, so a .env knob would be
# dead.
REPOST_FETCH_CAP = 120
REPOST_EXAMPLES = 30

MODEL = os.getenv("CLAUDE_MODEL", "haiku")
MAX_TWEETS = int(os.getenv("MAX_TWEETS", "150"))
HOURS_BACK = float(os.getenv("HOURS_BACK", "24"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))          # appears in the markdown digest
LIKE_MIN_SCORE = int(os.getenv("LIKE_MIN_SCORE", "8"))      # also liked
REPOST_MIN_SCORE = int(os.getenv("REPOST_MIN_SCORE", "9"))  # reposted to your timeline
FOLLOW_MIN_SCORE = int(os.getenv("FOLLOW_MIN_SCORE", "10")) # author followed
REPOST_MAX_PER_RUN = int(os.getenv("REPOST_MAX_PER_RUN", "25"))  # safety cap per run
LIKE_MAX_PER_RUN = int(os.getenv("LIKE_MAX_PER_RUN", "25"))      # safety cap per run
FOLLOW_MAX_PER_RUN = int(os.getenv("FOLLOW_MAX_PER_RUN", "10"))  # safety cap per run

SYSTEM = f"""You curate a daily reading list from an X timeline for one specific person.
Their persona follows; treat it as the rubric.

<persona>
{PERSONA}
</persona>

Hard filter, applied before anything else: this person only wants technical substance --
engineering, systems, code, architecture, research results and methods, benchmarks, how
things actually work, tools and APIs. Exclude anything whose value is commentary or
spectacle rather than technical content you can learn a method from, EVEN IF the topic
matches the persona: company or product policy complaints, AI hype or fear/doom about the
future, opinion, punditry, predictions, hot takes, career or business gossip, funding, and
drama. Three traps to reject specifically, because they imitate technical content:

- AI wow-demos and capability flexes: "look what the model did", "it built X in N minutes"
  posts that marvel at a result without explaining the method, showing numbers, or teaching
  how it works. An impressive output (a CAD model, a working app) is not substance; the how
  is. Drop these.
- Jokes, satire, memes, and bits, even when written in fluent technical vocabulary. Judge
  the tone and intent, not the keywords -- a sentence full of real ML terms can still be a
  gag. If it reads as a joke, drop it.
- Bare link-drops, announcements, and amplification: "here's a cool thing", "I shipped X",
  "great post by @y", where the actual content lives in the link and the tweet itself carries
  no standalone technical substance. A link is a plus only when the tweet states the concrete
  technical content; pointing at substance is not the same as containing it.

If a tweet is not technical, it does not qualify -- score it 2 or below so it drops out. When
unsure whether something is technical enough, leave it out.

You get a numbered list of tweets. For each tweet worth this person's reading time, emit a
pick with the tweet's index, a short theme (2-4 words, reuse themes across picks so they
group well), a 1-10 relevance score, one sentence on why it earns a spot, and a description.
Score 10 means "would have hunted this down anyway", 6 means "worth a skim". Skip everything
below 6; do not pad the list. Scores of 8 or above cause the tweet to be liked and reposted
on this person's public profile, so reserve 8-10 for tweets whose technical substance is in
the tweet itself -- a method, numbers, code, or a concrete explanation they can act on. A
tweet that merely points at substance elsewhere (a link, an announcement, "great post by @y")
tops out at 7 no matter how good the linked thing is, so it can still surface as a skim
without being amplified. Retweets count for their content, not the retweeter. Also report skipped_themes: the 3-5 topics that
dominated the timeline but did not make the cut, so the person can sanity-check the filter.

The description is a short, plain-language statement of what the tweet is actually about, in
simple words a tired engineer can skim. Strip every bit of fluff and every buzzword: no
"revolutionary", "game-changing", "essential", "powerful", "seamless", "frontier",
"comprehensive", "cutting-edge", "unlock", "leverage", "transform", "next-generation", no
breathless adjectives, no marketing. State only the concrete technical fact: what was built,
measured, or shown. If, once you remove the fluff and buzzwords, there is no concrete
technical substance left to state, return an empty string for description."""


class Pick(BaseModel):
    tweet_index: int
    theme: str
    score: int
    reason: str
    description: str


class Digest(BaseModel):
    picks: list[Pick]
    skipped_themes: list[str]


def authenticate(client: Client) -> None:
    """Authenticate with a browser session.

    Log in to X once in your browser (where your passkey works), copy the auth_token and
    ct0 cookies, and the script reuses that session. After the first run the cookies live
    in cookies.json, so the env vars are only needed once.
    """
    if COOKIES_FILE.exists():
        client.load_cookies(str(COOKIES_FILE))
        return

    auth_token = os.getenv("X_AUTH_TOKEN")
    ct0 = os.getenv("X_CT0")
    if not auth_token or not ct0:
        sys.exit(
            "No X session found. Set X_AUTH_TOKEN and X_CT0 in .env: log in to X in your "
            "browser with your passkey, then copy those two cookies from DevTools > "
            "Application > Cookies. See the README.")
    client.set_cookies({"auth_token": auth_token, "ct0": ct0})
    client.save_cookies(str(COOKIES_FILE))


async def fetch_timeline(client: Client) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    tweets: list = []
    page = await client.get_latest_timeline(count=40)
    while page and len(tweets) < MAX_TWEETS:
        hit_cutoff = False
        for tweet in page:
            created = getattr(tweet, "created_at_datetime", None)
            if created and created < cutoff:
                hit_cutoff = True
                continue
            tweets.append(tweet)
        if hit_cutoff or not len(page):
            break
        page = await page.next()
    return tweets[:MAX_TWEETS]


async def fetch_my_reposts(client: Client) -> list | None:
    """Mirror the reposts currently visible on the authenticated user's own profile.

    The profile 'Tweets' tab includes the user's retweets, so we page through it (latest to
    oldest, bounded by REPOST_FETCH_CAP) and keep the items that are reposts -- an item is a
    repost when ``retweeted_tweet`` is populated, the same accessor used elsewhere here. For
    each we capture the *source* tweet's content. Older reposts beyond this window simply do
    not appear, which is acceptable.

    Returns a list of dicts (possibly empty if the user currently has no visible reposts), or
    None on any twikit error -- distinct from an empty list so the caller can tell 'fetch
    failed' from 'fetched, zero reposts' and avoid wiping a good DB on a transient error.
    """
    try:
        try:
            uid = await client.user_id()
        except Exception:
            # Older twikit may not expose user_id(); fall back via the logged-in handle.
            handle = os.getenv("X_SCREEN_NAME")
            if not handle:
                print("  reposts: could not resolve own user id (set X_SCREEN_NAME to enable)")
                return None
            uid = str((await client.get_user_by_screen_name(handle)).id)

        reposts: list = []
        seen: set[str] = set()
        page = await client.get_user_tweets(uid, "Tweets", count=40)
        while page and len(reposts) < REPOST_FETCH_CAP:
            for item in page:
                source = getattr(item, "retweeted_tweet", None)
                if not source:
                    continue
                sid = str(source.id)
                if sid in seen:
                    continue
                seen.add(sid)
                author = source.user
                reposts.append({
                    "id": sid,
                    "handle": author.screen_name,
                    "author_id": str(author.id),
                    "text": tweet_text(source),
                    "created_at": getattr(source, "created_at", None),
                    "url": f"https://x.com/{author.screen_name}/status/{sid}",
                })
                if len(reposts) >= REPOST_FETCH_CAP:
                    break
            if len(reposts) >= REPOST_FETCH_CAP:
                break
            nxt = await page.next()
            if not nxt or not len(nxt):
                break
            page = nxt
        return reposts
    except Exception as err:
        print(f"  reposts fetch failed: {_first_line(err)}")
        return None


def load_state() -> tuple[set[str], set[str], set[str]]:
    """Ids already reposted / liked (tweets) and followed (users), so daily runs don't
    act on the same thing twice."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return (set(data.get("reposted", [])), set(data.get("liked", [])),
                    set(data.get("followed", [])))
        except (ValueError, OSError):
            pass
    return set(), set(), set()


def save_state(reposted: set[str], liked: set[str], followed: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(
        {"reposted": sorted(reposted), "liked": sorted(liked), "followed": sorted(followed)},
        indent=2))


def load_reposts() -> dict[str, dict]:
    """The reposts DB: source-tweet-id -> {handle, author_id, text, created_at, url}.

    A mirror of the reposts currently visible on your profile, rebuilt each run from
    save_reposts. Personal content, so it lives in the gitignored reposts.json.
    """
    if REPOSTS_FILE.exists():
        try:
            data = json.loads(REPOSTS_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def save_reposts(reposts: list[dict]) -> None:
    """Rebuild reposts.json to exactly the fetched set (mirror, not merge).

    Reposts the user removed, or that fell outside the fetched window, are dropped and stop
    counting toward preferences -- the DB reflects the user's reposts as they stand now.
    Only call this when the fetch SUCCEEDED; on a fetch error leave the file untouched so a
    transient hiccup never silently empties the DB.
    """
    db = {r["id"]: {k: r[k] for k in ("handle", "author_id", "text", "created_at", "url")}
          for r in reposts}
    REPOSTS_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


def _first_line(err: Exception) -> str:
    text = str(err).splitlines()
    return (text[0] if text else repr(err))[:120]


async def act_on_picks(client: Client, tweets: list, digest: Digest) -> tuple[int, int, int]:
    """Repost picks scoring REPOST_MIN_SCORE+, like those at LIKE_MIN_SCORE+, and follow
    the author of anything at FOLLOW_MIN_SCORE+.

    This is what turns your profile into the feed you read: highest-confidence picks get
    reposted (and the very best liked), and authors of a perfect-score tweet get followed
    so the next ones reach you directly. New follows come mostly from reposts in your feed
    of accounts you don't already follow. Anything already acted on is skipped. Each action
    type has its own per-run cap (REPOST/LIKE/FOLLOW_MAX_PER_RUN) counted over the actions
    actually taken, so hitting one cap does not stop the others.
    """
    reposted, liked, followed = load_state()
    new_reposts = new_likes = new_follows = 0
    # Consider every pick that clears any action's threshold. Each cap below limits the
    # number of *new* actions actually taken, counted after the per-tweet filtering
    # (already acted on, already reposted/favorited, already following), so one action
    # type reaching its cap never stops the others from being applied.
    threshold = min(REPOST_MIN_SCORE, LIKE_MIN_SCORE, FOLLOW_MIN_SCORE)
    candidates = sorted(
        (p for p in digest.picks
         if 0 <= p.tweet_index < len(tweets) and p.score >= threshold),
        key=lambda p: -p.score)

    for pick in candidates:
        if new_reposts >= REPOST_MAX_PER_RUN and new_likes >= LIKE_MAX_PER_RUN \
                and new_follows >= FOLLOW_MAX_PER_RUN:
            break  # every cap reached; nothing left to do
        tweet = tweets[pick.tweet_index]
        source = getattr(tweet, "retweeted_tweet", None) or tweet
        tid, author = str(source.id), source.user
        handle, uid = author.screen_name, str(author.id)

        if pick.score >= REPOST_MIN_SCORE and new_reposts < REPOST_MAX_PER_RUN \
                and tid not in reposted and not getattr(source, "retweeted", False):
            try:
                await client.retweet(tid)
                reposted.add(tid)
                new_reposts += 1
                print(f"  reposted @{handle} ({pick.score}/10)")
                await asyncio.sleep(2)
            except Exception as err:
                print(f"  repost failed for @{handle}: {_first_line(err)}")

        if pick.score >= LIKE_MIN_SCORE and new_likes < LIKE_MAX_PER_RUN \
                and tid not in liked and not getattr(source, "favorited", False):
            try:
                await client.favorite_tweet(tid)
                liked.add(tid)
                new_likes += 1
                print(f"  liked @{handle} ({pick.score}/10)")
                await asyncio.sleep(2)
            except Exception as err:
                print(f"  like failed for @{handle}: {_first_line(err)}")

        if pick.score >= FOLLOW_MIN_SCORE and new_follows < FOLLOW_MAX_PER_RUN \
                and uid not in followed and getattr(author, "following", None) is not True:
            try:
                await client.follow_user(uid)
                followed.add(uid)
                new_follows += 1
                print(f"  followed @{handle} (scored a 10)")
                await asyncio.sleep(2)
            except Exception as err:
                print(f"  follow failed for @{handle}: {_first_line(err)}")

    if new_reposts or new_likes or new_follows:
        save_state(reposted, liked, followed)
    return new_reposts, new_likes, new_follows


def tweet_url(tweet) -> str:
    return f"https://x.com/{tweet.user.screen_name}/status/{tweet.id}"


def tweet_text(tweet) -> str:
    return getattr(tweet, "full_text", None) or tweet.text or ""


def tweet_links(tweet) -> list[str]:
    links = []
    for url in getattr(tweet, "urls", None) or []:
        expanded = url.get("expanded_url") if isinstance(url, dict) else url
        if expanded and "x.com" not in expanded and "twitter.com" not in expanded:
            links.append(expanded)
    return links


def render_for_model(index: int, tweet) -> str:
    retweeted = getattr(tweet, "retweeted_tweet", None)
    source = retweeted or tweet
    lines = [f"[{index}] @{source.user.screen_name}"
             + (f" (retweeted by @{tweet.user.screen_name})" if retweeted else "")]
    lines.append(tweet_text(source))
    quoted = getattr(source, "quote", None)
    if quoted:
        lines.append(f"quoting @{quoted.user.screen_name}: {tweet_text(quoted)[:280]}")
    links = tweet_links(source)
    if links:
        lines.append("links: " + " ".join(links))
    likes = getattr(source, "favorite_count", 0) or 0
    reposts = getattr(source, "retweet_count", 0) or 0
    lines.append(f"engagement: {likes} likes, {reposts} reposts")
    return "\n".join(lines)


def extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        sys.exit(f"claude returned no JSON, got: {text.strip()[:300]}")
    return text[start:end + 1]


def render_reposts_block(reposts: dict[str, dict]) -> str:
    """A clearly-delimited block of the user's recent reposts as positive calibration, or ""
    when the DB is empty (so behavior is then identical to having no block at all)."""
    if not reposts:
        return ""
    # Dict order is insertion order; load_reposts preserves the fetch's latest-first order.
    examples = list(reposts.values())[:REPOST_EXAMPLES]
    lines = []
    for r in examples:
        text = " ".join((r.get("text") or "").split())
        if len(text) > 280:
            text = text[:280] + "..."
        lines.append(f"@{r.get('handle', '?')}: {text}")
    body = "\n".join(lines)
    return (
        "\n<reposts_i_made>\n"
        "These are tweets this person actually reposted to their own public profile -- "
        "concrete, ground-truth examples of what clears the bar. Treat them as positive "
        "calibration: a candidate matching their substance and style should score high. "
        "They do NOT override the hard technical-only filter or the persona above.\n\n"
        f"{body}\n"
        "</reposts_i_made>\n"
    )


def score_tweets(tweets: list, reposts: dict[str, dict] | None = None) -> Digest:
    if shutil.which("claude") is None:
        sys.exit("claude CLI not found - install Claude Code first: https://code.claude.com")
    payload = "\n\n---\n\n".join(render_for_model(i, t) for i, t in enumerate(tweets))
    schema = json.dumps(Digest.model_json_schema())
    prompt = (
        f"{SYSTEM}\n"
        f"{render_reposts_block(reposts or {})}\n"
        f"Reply with one JSON object matching this schema. No prose, no code fences.\n"
        f"{schema}\n\n"
        f"The tweets:\n\n{payload}"
    )
    result = subprocess.run(
        ["claude", "-p", "--model", MODEL],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        sys.exit(f"claude -p failed: {(result.stderr or result.stdout).strip()[:500]}")
    try:
        return Digest.model_validate_json(extract_json(result.stdout))
    except ValidationError as err:
        sys.exit(f"claude returned JSON that does not match the schema: {err}")


def write_digest(tweets: list, digest: Digest, reposts: int, likes: int, follows: int) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    reposted, liked, followed = load_state()
    valid = [p for p in digest.picks
             if 0 <= p.tweet_index < len(tweets) and p.score >= MIN_SCORE
             and p.description.strip()]
    by_theme: dict[str, list[Pick]] = {}
    for pick in sorted(valid, key=lambda p: -p.score):
        by_theme.setdefault(pick.theme, []).append(pick)

    lines = [f"# Reading list, {today}", ""]
    lines.append(f"{len(valid)} picks from {len(tweets)} tweets in the last {HOURS_BACK:g} hours; "
                 f"reposted {reposts}, liked {likes}, followed {follows} new account(s).")
    lines.append("")
    for theme, picks in sorted(by_theme.items(), key=lambda kv: -max(p.score for p in kv[1])):
        lines.append(f"## {theme}")
        lines.append("")
        for pick in picks:
            tweet = tweets[pick.tweet_index]
            source = getattr(tweet, "retweeted_tweet", None) or tweet
            tid = str(source.id)
            tags = []
            if tid in reposted:
                tags.append("reposted")
            if tid in liked:
                tags.append("liked")
            if str(source.user.id) in followed:
                tags.append("followed")
            tag = f" _({', '.join(tags)})_" if tags else ""
            snippet = " ".join(tweet_text(source).split())
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(f"- **@{source.user.screen_name}** ({pick.score}/10){tag} {pick.reason}")
            lines.append(f"  {pick.description.strip()}")
            lines.append(f"  > {snippet}")
            lines.append(f"  {tweet_url(source)}")
            for link in tweet_links(source):
                lines.append(f"  {link}")
            lines.append("")
    if digest.skipped_themes:
        lines.append("## Skipped")
        lines.append("")
        lines.append("Dominated the timeline but did not make the cut: "
                     + ", ".join(digest.skipped_themes) + ".")
        lines.append("")

    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"{today}.md"
    path.write_text("\n".join(lines))
    return path


async def run() -> Path | None:
    client = Client("en-US")
    authenticate(client)
    print("Mirroring your reposts from your profile...")
    fetched = await fetch_my_reposts(client)
    if fetched is not None:
        save_reposts(fetched)  # rebuild reposts.json to exactly what is visible now
        print(f"  reposts DB: {len(fetched)} repost(s) currently visible")
    else:
        print("  reposts fetch failed; keeping the existing reposts.json")
    reposts = load_reposts()
    print(f"Fetching timeline (last {HOURS_BACK:g}h, max {MAX_TWEETS} tweets)...")
    tweets = await fetch_timeline(client)
    if not tweets:
        return None
    print(f"Scoring {len(tweets)} tweets against persona.md"
          f"{f' + {len(reposts)} reposts' if reposts else ''}...")
    digest = score_tweets(tweets, reposts)
    print(f"Reposting {REPOST_MIN_SCORE}+, liking {LIKE_MIN_SCORE}+, following {FOLLOW_MIN_SCORE}+ ...")
    reposts, likes, follows = await act_on_picks(client, tweets, digest)
    return write_digest(tweets, digest, reposts, likes, follows)


def main() -> None:
    load_dotenv(ROOT / ".env")
    path = asyncio.run(run())
    if path is None:
        sys.exit("No tweets found in the window; nothing to digest.")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
