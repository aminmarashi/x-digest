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
LIKES_FILE = ROOT / "likes.json"
DISLIKES_FILE = ROOT / "dislikes.json"
DIGEST_DIR = ROOT / "digests"
PERSONA = (ROOT / "persona.md").read_text()

# How many of your own reposts to fetch (cap, kept small to stay polite to X) and how many
# to feed into the scoring prompt as positive calibration. Plain module constants, NOT env
# knobs: module-level os.getenv runs at import, before load_dotenv, so a .env knob would be
# dead.
REPOST_FETCH_CAP = 120
REPOST_EXAMPLES = 30
LIKE_FETCH_CAP = 120
DISLIKE_EXAMPLES = 30   # how many false positives to feed the scorer (prompt bound only)

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
        uid = await _resolve_uid(client)
        if uid is None:
            print("  reposts: could not resolve own user id (set X_SCREEN_NAME to enable)")
            return None

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


async def _resolve_uid(client: Client) -> str | None:
    """The authenticated user's own numeric id, or None if it cannot be resolved.

    Prefers client.user_id(); older twikit may not expose it, so fall back to the logged-in
    handle from X_SCREEN_NAME. Shared by the reposts and likes fetchers.
    """
    try:
        return await client.user_id()
    except Exception:
        handle = os.getenv("X_SCREEN_NAME")
        if not handle:
            return None
        return str((await client.get_user_by_screen_name(handle)).id)


async def fetch_my_likes(client: Client) -> list | None:
    """Mirror the likes currently visible on the authenticated user's own profile.

    Structurally identical to fetch_my_reposts, but pages the profile 'Likes' tab. Every item
    on that tab IS a liked tweet, so the item itself is the source (no retweeted_tweet unwrap).
    Bounded by LIKE_FETCH_CAP; older likes beyond the window simply do not appear.

    Returns a list of dicts (possibly empty if no visible likes), or None on any twikit error
    -- the same fetch-failed-vs-empty contract as reposts, so the caller never wipes a good DB
    on a transient error.
    """
    try:
        uid = await _resolve_uid(client)
        if uid is None:
            print("  likes: could not resolve own user id (set X_SCREEN_NAME to enable)")
            return None

        likes: list = []
        seen: set[str] = set()
        page = await client.get_user_tweets(uid, "Likes", count=40)
        while page and len(likes) < LIKE_FETCH_CAP:
            for item in page:
                sid = str(item.id)
                if sid in seen:
                    continue
                seen.add(sid)
                author = item.user
                likes.append({
                    "id": sid,
                    "handle": author.screen_name,
                    "author_id": str(author.id),
                    "text": tweet_text(item),
                    "created_at": getattr(item, "created_at", None),
                    "url": f"https://x.com/{author.screen_name}/status/{sid}",
                })
                if len(likes) >= LIKE_FETCH_CAP:
                    break
            if len(likes) >= LIKE_FETCH_CAP:
                break
            nxt = await page.next()
            if not nxt or not len(nxt):
                break
            page = nxt
        return likes
    except Exception as err:
        print(f"  likes fetch failed: {_first_line(err)}")
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


def load_likes() -> dict[str, dict]:
    """The likes DB: source-tweet-id -> {handle, author_id, text, created_at, url}.

    A mirror of the likes currently visible on your profile, rebuilt each run from save_likes.
    Personal content, so it lives in the gitignored likes.json. Same shape as load_reposts.
    """
    if LIKES_FILE.exists():
        try:
            data = json.loads(LIKES_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def save_likes(likes: list[dict]) -> None:
    """Rebuild likes.json to exactly the fetched set (mirror, not merge).

    Likes the user removed, or that fell outside the fetched window, are dropped -- the DB
    reflects the user's likes as they stand now. Only call this when the fetch SUCCEEDED so a
    transient hiccup never silently empties the DB. Byte-for-byte the save_reposts pattern.
    """
    db = {r["id"]: {k: r[k] for k in ("handle", "author_id", "text", "created_at", "url")}
          for r in likes}
    LIKES_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


def load_dislikes() -> dict[str, dict]:
    """The dislikes DB: tweet-id -> {handle, author_id, text, url, source, undone_at}.

    An accumulating, UNBOUNDED record of tweets the user reposted or liked then UNDID -- strong
    negative signal (false positives). It is the durable false-positive blocklist, so every id
    is retained for action-blocking; only the rendered prompt examples are capped. Gitignored.
    """
    if DISLIKES_FILE.exists():
        try:
            data = json.loads(DISLIKES_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def save_dislikes(db: dict[str, dict]) -> None:
    DISLIKES_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


def detect_undones(prev: dict[str, dict], fresh: list | None, source: str,
                   capped: bool, now: str) -> dict[str, dict]:
    """Ids present in the PREVIOUS snapshot but gone from the FRESH fetch -- i.e. undone.

    Pure and testable (takes the clock as `now`). Returns {} when we cannot trust a
    disappearance as an undo:
    - fresh is None (fetch failed): cannot tell an undo from an outage.
    - capped (the fetch hit its cap, so coverage is partial): a disappearance could be aging
      out of the window rather than an undo. Keeping the signal STRONG matters more than catching
      every undo, so we skip the whole diff in this case.
    Otherwise each disappeared id yields an entry built from the PREVIOUS snapshot (which still
    has its text), tagged with `source` ("repost"/"like") and `undone_at = now`.
    """
    if fresh is None or capped:
        return {}
    fresh_ids = {r["id"] for r in fresh}
    undone: dict[str, dict] = {}
    for tid in set(prev) - fresh_ids:
        entry = prev[tid]
        undone[tid] = {
            "handle": entry.get("handle"),
            "author_id": entry.get("author_id"),
            "text": entry.get("text"),
            "url": entry.get("url"),
            "source": source,
            "undone_at": now,
        }
    return undone


def merge_dislikes(db: dict[str, dict], undones: dict[str, dict],
                   current_ids: set[str]) -> dict[str, dict]:
    """Fold newly-detected undones into the dislikes DB.

    - Drop any id that is back on the profile (in current_ids): re-endorsed, no longer a false
      positive.
    - Add new undones, keeping the EARLIEST undone_at if an id recurs.
    - Never cap the DB: every retained id is a durable action-block. Bounding happens only at
      render time. Undos are rare, so unbounded growth is a non-issue.
    """
    merged = {tid: entry for tid, entry in db.items() if tid not in current_ids}
    for tid, entry in undones.items():
        if tid in current_ids:
            continue
        existing = merged.get(tid)
        if existing and existing.get("undone_at") and entry.get("undone_at"):
            # Keep the earliest first-seen timestamp.
            if existing["undone_at"] <= entry["undone_at"]:
                continue
        merged[tid] = entry
    return merged


def _first_line(err: Exception) -> str:
    text = str(err).splitlines()
    return (text[0] if text else repr(err))[:120]


async def act_on_picks(client: Client, tweets: list, digest: Digest) -> tuple[int, int, int, dict]:
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
    dislikes = load_dislikes()  # durable false-positive blocklist; never re-act on these
    new_reposts = new_likes = new_follows = 0
    # This run's successful endorsements, in the same shape as fetch_my_reposts/fetch_my_likes,
    # so run() can fold them into the saved mirrors and detect a same-cycle endorse-then-undo.
    endorsed: dict[str, dict] = {"reposts": {}, "likes": {}}
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
                and tid not in reposted and tid not in dislikes \
                and not getattr(source, "retweeted", False):
            try:
                await client.retweet(tid)
                reposted.add(tid)
                new_reposts += 1
                endorsed["reposts"][tid] = _endorsement_entry(tid, handle, uid, source)
                print(f"  reposted @{handle} ({pick.score}/10)")
                await asyncio.sleep(2)
            except Exception as err:
                print(f"  repost failed for @{handle}: {_first_line(err)}")

        if pick.score >= LIKE_MIN_SCORE and new_likes < LIKE_MAX_PER_RUN \
                and tid not in liked and tid not in dislikes \
                and not getattr(source, "favorited", False):
            try:
                await client.favorite_tweet(tid)
                liked.add(tid)
                new_likes += 1
                endorsed["likes"][tid] = _endorsement_entry(tid, handle, uid, source)
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
    return new_reposts, new_likes, new_follows, endorsed


def _endorsement_entry(tid: str, handle: str, uid: str, source) -> dict:
    """A repost/like the script just made, in the same shape (incl. id) as fetch_my_reposts, so
    it folds straight into a mirror keyed by r["id"]."""
    return {
        "id": tid,
        "handle": handle,
        "author_id": uid,
        "text": tweet_text(source),
        "created_at": getattr(source, "created_at", None),
        "url": tweet_url(source),
    }


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


def render_dislikes_block(dislikes: dict[str, dict]) -> str:
    """A clearly-delimited block of the user's undone reposts/likes as NEGATIVE calibration, or
    "" when the DB is empty (so behavior is then identical to having no block at all).

    Only the most recent DISLIKE_EXAMPLES entries (by undone_at) are rendered; the stored
    blocklist itself is never capped.
    """
    if not dislikes:
        return ""
    examples = sorted(
        dislikes.values(),
        key=lambda d: d.get("undone_at") or "",
        reverse=True,
    )[:DISLIKE_EXAMPLES]
    lines = []
    for d in examples:
        text = " ".join((d.get("text") or "").split())
        if len(text) > 280:
            text = text[:280] + "..."
        lines.append(f"@{d.get('handle', '?')}: {text}")
    body = "\n".join(lines)
    return (
        "\n<false_positives>\n"
        "These are tweets this person reposted or liked and then REMOVED from their profile -- "
        "ground-truth false positives they did NOT actually want. Treat them as negative "
        "calibration: a candidate resembling these in substance or style should score LOWER. "
        "They do NOT override the hard technical-only filter or the persona above.\n\n"
        f"{body}\n"
        "</false_positives>\n"
    )


def score_tweets(tweets: list, reposts: dict[str, dict] | None = None,
                 dislikes: dict[str, dict] | None = None) -> Digest:
    if shutil.which("claude") is None:
        sys.exit("claude CLI not found - install Claude Code first: https://code.claude.com")
    payload = "\n\n---\n\n".join(render_for_model(i, t) for i, t in enumerate(tweets))
    schema = json.dumps(Digest.model_json_schema())
    prompt = (
        f"{SYSTEM}\n"
        f"{render_reposts_block(reposts or {})}\n"
        f"{render_dislikes_block(dislikes or {})}\n"
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

    # Snapshot what was on the profile last run BEFORE this run mutates anything, then fetch the
    # fresh state. An id present last run but gone now (within the fetch window) was undone.
    prev_reposts = load_reposts()
    prev_likes = load_likes()
    print("Mirroring your reposts and likes from your profile...")
    fetched_reposts = await fetch_my_reposts(client)
    fetched_likes = await fetch_my_likes(client)

    # Detect undos by diffing the previous snapshot against the fresh fetch (per type), then fold
    # them into the durable dislikes blocklist. Re-present ids are dropped from the blocklist.
    now = datetime.now(timezone.utc).isoformat()
    undone: dict[str, dict] = {}
    for prev, fresh, src, cap in (
            (prev_reposts, fetched_reposts, "repost", REPOST_FETCH_CAP),
            (prev_likes, fetched_likes, "like", LIKE_FETCH_CAP)):
        if fresh is None:
            continue
        capped = len(fresh) >= cap
        if capped:
            print(f"  {src}s: fetch hit the cap ({cap}); skipping undo detection this run")
            continue
        found = detect_undones(prev, fresh, src, capped, now)
        if found:
            print(f"  {len(found)} undone {src}(s) captured as negative signals")
        undone.update(found)
    current_ids: set[str] = set()
    for fresh in (fetched_reposts, fetched_likes):
        if fresh is not None:
            current_ids |= {r["id"] for r in fresh}
    dislikes = merge_dislikes(load_dislikes(), undone, current_ids)
    save_dislikes(dislikes)

    # Early mirror save, so an empty timeline still persists the fresh fetches.
    if fetched_reposts is not None:
        save_reposts(fetched_reposts)  # rebuild reposts.json to exactly what is visible now
        print(f"  reposts DB: {len(fetched_reposts)} repost(s) currently visible")
    else:
        print("  reposts fetch failed; keeping the existing reposts.json")
    if fetched_likes is not None:
        save_likes(fetched_likes)
        print(f"  likes DB: {len(fetched_likes)} like(s) currently visible")
    else:
        print("  likes fetch failed; keeping the existing likes.json")

    reposts_view = load_reposts()  # dict-by-id mirror shape render_reposts_block expects
    print(f"Fetching timeline (last {HOURS_BACK:g}h, max {MAX_TWEETS} tweets)...")
    tweets = await fetch_timeline(client)
    if not tweets:
        return None
    extras = []
    if reposts_view:
        extras.append(f"{len(reposts_view)} reposts")
    if dislikes:
        extras.append(f"{len(dislikes)} false-positives")
    print(f"Scoring {len(tweets)} tweets against persona.md"
          f"{' + ' + ' / '.join(extras) if extras else ''}...")
    digest = score_tweets(tweets, reposts_view, dislikes)
    print(f"Reposting {REPOST_MIN_SCORE}+, liking {LIKE_MIN_SCORE}+, following {FOLLOW_MIN_SCORE}+ ...")
    n_re, n_li, n_fo, endorsed = await act_on_picks(client, tweets, digest)

    # Re-save the mirrors with this run's endorsements folded in, so next run can detect their
    # undo. Skip any type whose fetch failed (mirror left untouched -- a residual blind spot
    # that self-heals on the next successful fetch).
    if fetched_reposts is not None and endorsed["reposts"]:
        save_reposts(list({**{r["id"]: r for r in fetched_reposts},
                           **endorsed["reposts"]}.values()))
    if fetched_likes is not None and endorsed["likes"]:
        save_likes(list({**{r["id"]: r for r in fetched_likes},
                         **endorsed["likes"]}.values()))

    return write_digest(tweets, digest, n_re, n_li, n_fo)


def main() -> None:
    load_dotenv(ROOT / ".env")
    path = asyncio.run(run())
    if path is None:
        sys.exit("No tweets found in the window; nothing to digest.")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
