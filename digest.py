"""Fetch your X timeline, score it against persona.md with opencode, write a daily reading list.

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
OWN_ACTIONS_FILE = ROOT / "own_actions.json"
DIGEST_DIR = ROOT / "digests"
PERSONA = (ROOT / "persona.md").read_text()

# Reconciliation works inside a recency window: we compare what the SCRIPT did against what is
# actually on the profile, but only for tweets created in the last WINDOW_DAYS. FETCH_SAFETY_CAP
# is a high paging ceiling (a runaway-guard, NOT the logical bound) so the windowed fetch can
# fully cover the window -- the user likes ~120+/week, far under 1000. REPOST/LIKE_EXAMPLES and
# DISLIKE_EXAMPLES bound only how many examples reach the scoring prompt, never the stored sets.
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))
FETCH_SAFETY_CAP = int(os.getenv("FETCH_SAFETY_CAP", "1000"))
REPOST_EXAMPLES = 30
DISLIKE_EXAMPLES = 30   # how many false positives to feed the scorer (prompt bound only)

MODEL = os.getenv("OPENCODE_MODEL", "ollama-cloud/deepseek-v4-flash")
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
    try:
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
    except Exception as err:
        # A flaky X response (e.g. twikit KeyError on a transient error envelope) must not
        # abort the digest: keep whatever we paged so far. If the very first call failed we
        # return [] and run() exits cleanly with its "No tweets" message.
        print(f"  timeline fetch interrupted ({_first_line(err)}); "
              f"proceeding with {len(tweets)} tweet(s)")
    return tweets[:MAX_TWEETS]


def in_window(created_at, now: datetime, days: int = WINDOW_DAYS) -> bool:
    """Is the X ``created_at`` string within ``days`` of ``now``?

    X formats timestamps like "Sat Jun 20 18:16:13 +0000 2026". On a parse failure (or a
    falsy value) we return True -- keep the item rather than silently drop it on a format
    hiccup; a stray extra is harmless, a silently-dropped real action is not.
    """
    if not created_at:
        return True
    try:
        ts = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return True
    return ts >= now - timedelta(days=days)


async def fetch_my_reposts(client: Client) -> list | None:
    """Mirror the in-window reposts currently visible on the authenticated user's own profile.

    The profile 'Tweets' tab includes the user's retweets, so we page through it (latest to
    oldest) and keep the reposts whose source tweet was created within WINDOW_DAYS -- an item
    is a repost when ``retweeted_tweet`` is populated. The Likes/Tweets tabs are NOT ordered
    monotonically by source ``created_at``, so a single out-of-window page does NOT prove full
    coverage; we page until the tab is EXHAUSTED (``page.next()`` yields nothing) and keep only
    the in-window items.

    Returns a list of dicts (possibly empty), or None on a twikit error OR when FETCH_SAFETY_CAP
    items are scanned before the tab is exhausted (coverage incomplete -- we cannot trust a
    partial snapshot, so the caller skips reconciliation for this type and keeps the old file).
    """
    try:
        uid = await _resolve_uid(client)
        if uid is None:
            print("  reposts: could not resolve own user id (set X_SCREEN_NAME to enable)")
            return None

        now = datetime.now(timezone.utc)
        reposts: list = []
        seen: set[str] = set()
        scanned = 0
        page = await client.get_user_tweets(uid, "Tweets", count=40)
        while page:
            for item in page:
                scanned += 1
                source = getattr(item, "retweeted_tweet", None)
                if not source:
                    continue
                sid = str(source.id)
                if sid in seen:
                    continue
                seen.add(sid)
                created = getattr(source, "created_at", None)
                if not in_window(created, now):
                    continue
                author = source.user
                reposts.append({
                    "id": sid,
                    "handle": author.screen_name,
                    "author_id": str(author.id),
                    "text": tweet_text(source),
                    "created_at": created,
                    "url": f"https://x.com/{author.screen_name}/status/{sid}",
                })
            nxt = await page.next()
            if not nxt or not len(nxt):
                return reposts  # tab exhausted -> the window is fully covered
            if scanned >= FETCH_SAFETY_CAP:
                print(f"  reposts: exceeded safety cap {FETCH_SAFETY_CAP} before covering the "
                      f"{WINDOW_DAYS}-day window; skipping reposts reconciliation this run")
                return None
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
    """Mirror the in-window likes currently visible on the authenticated user's own profile.

    Structurally identical to fetch_my_reposts, but pages the profile 'Likes' tab. Every item
    on that tab IS a liked tweet, so the item itself is the source (no retweeted_tweet unwrap).
    Keeps only items created within WINDOW_DAYS and, because the tab is not monotonically
    ordered by created_at, pages until the tab is EXHAUSTED.

    Returns a list of dicts (possibly empty), or None on a twikit error OR when FETCH_SAFETY_CAP
    items are scanned before the tab is exhausted (coverage incomplete) -- the same skip-and-keep
    contract as reposts, so the caller never reconciles against a partial snapshot.
    """
    try:
        uid = await _resolve_uid(client)
        if uid is None:
            print("  likes: could not resolve own user id (set X_SCREEN_NAME to enable)")
            return None

        now = datetime.now(timezone.utc)
        likes: list = []
        seen: set[str] = set()
        scanned = 0
        page = await client.get_user_tweets(uid, "Likes", count=40)
        while page:
            for item in page:
                scanned += 1
                sid = str(item.id)
                if sid in seen:
                    continue
                seen.add(sid)
                created = getattr(item, "created_at", None)
                if not in_window(created, now):
                    continue
                author = item.user
                likes.append({
                    "id": sid,
                    "handle": author.screen_name,
                    "author_id": str(author.id),
                    "text": tweet_text(item),
                    "created_at": created,
                    "url": f"https://x.com/{author.screen_name}/status/{sid}",
                })
            nxt = await page.next()
            if not nxt or not len(nxt):
                return likes  # tab exhausted -> the window is fully covered
            if scanned >= FETCH_SAFETY_CAP:
                print(f"  likes: exceeded safety cap {FETCH_SAFETY_CAP} before covering the "
                      f"{WINDOW_DAYS}-day window; skipping likes reconciliation this run")
                return None
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


def load_own_actions() -> dict[str, dict]:
    """What the SCRIPT itself reposted/liked: {"reposts": {id: entry}, "likes": {id: entry}}.

    Each entry is {id, handle, author_id, text, created_at, url, acted_at}. This is the ledger
    of the script's OWN actions, separate from the profile mirrors; reconcile() diffs it against
    the live profile to tell apart undos (false positives) from manual additions (false
    negatives). Personal, so it lives in the gitignored own_actions.json.
    """
    if OWN_ACTIONS_FILE.exists():
        try:
            data = json.loads(OWN_ACTIONS_FILE.read_text())
            if isinstance(data, dict):
                return {"reposts": dict(data.get("reposts") or {}),
                        "likes": dict(data.get("likes") or {})}
        except (ValueError, OSError):
            pass
    return {"reposts": {}, "likes": {}}


def prune_own_actions(own: dict[str, dict], now: datetime) -> dict[str, dict]:
    """Drop own-action entries whose tweet created_at has aged out of the window.

    Own actions are only tracked while their tweet is in-window (the same bound as the windowed
    fetch), so an action that ages out simply stops being reconciled -- acceptable.
    """
    for t in ("reposts", "likes"):
        own[t] = {tid: e for tid, e in own.get(t, {}).items()
                  if in_window(e.get("created_at"), now)}
    return own


def save_own_actions(own: dict[str, dict]) -> None:
    OWN_ACTIONS_FILE.write_text(json.dumps(
        prune_own_actions(own, datetime.now(timezone.utc)), indent=2, ensure_ascii=False))


def backfill_own_actions(own: dict[str, dict], actions_reposted: set[str],
                         actions_liked: set[str], profile_reposts: dict | None,
                         profile_likes: dict | None) -> dict[str, dict]:
    """Seed own-actions from the action history so the script's own past picks are not misread
    as manual additions (false negatives).

    For each type, every id in the matching actions.json set that is currently ON the profile
    (present in the windowed mirror) but MISSING from own[type] is seeded from the profile entry,
    with acted_at = the tweet's created_at. Because the profile fetch is windowed, only in-window
    historical picks get seeded -- consistent with both sides being windowed. Ids absent from the
    profile mirror are left alone (out of window, or genuinely undone). Skips a type whose profile
    fetch is None.
    """
    for t, action_ids, profile in (
            ("reposts", actions_reposted, profile_reposts),
            ("likes", actions_liked, profile_likes)):
        if profile is None:
            continue
        for tid in action_ids:
            if tid in profile and tid not in own[t]:
                entry = profile[tid]
                own[t][tid] = {
                    "id": tid,
                    "handle": entry.get("handle"),
                    "author_id": entry.get("author_id"),
                    "text": entry.get("text"),
                    "created_at": entry.get("created_at"),
                    "url": entry.get("url"),
                    "acted_at": entry.get("created_at"),
                }
    return own


def reconcile(own: dict[str, dict], profile_reposts: dict | None, profile_likes: dict | None,
              dislikes: dict[str, dict], now_iso: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Diff the script's own actions against the live profile within the window.

    For each type whose profile fetch succeeded (None means skip that type both ways):
    - FALSE POSITIVE: an own[type] id NOT on the profile -> the user undid it. Add it to
      `dislikes` (built from the own entry, which still has the text), tagged source/undone_at,
      and DROP it from own[type] so it is not re-flagged.
    - FALSE NEGATIVE: a profile id NOT in own[type] (after backfill) -> the user added it
      manually. Collected into false_negatives[type] (the strongest want-signal).
    Any dislikes id that is back on EITHER profile set is dropped (re-endorsed). `dislikes` is
    never capped. Returns (updated dislikes, false_negatives); neither own nor dislikes is
    persisted here -- the caller does that.
    """
    dislikes = dict(dislikes)
    false_negatives: dict[str, dict] = {"reposts": {}, "likes": {}}
    for t, source, profile in (
            ("reposts", "repost", profile_reposts),
            ("likes", "like", profile_likes)):
        if profile is None:
            continue
        present_ids = set(profile)
        for tid in list(own[t]):
            if tid not in present_ids:
                entry = own[t][tid]
                dislikes[tid] = {
                    "handle": entry.get("handle"),
                    "author_id": entry.get("author_id"),
                    "text": entry.get("text"),
                    "url": entry.get("url"),
                    "source": source,
                    "undone_at": now_iso,
                }
                del own[t][tid]
        false_negatives[t] = {tid: e for tid, e in profile.items() if tid not in own[t]}

    current_ids: set[str] = set()
    for profile in (profile_reposts, profile_likes):
        if profile is not None:
            current_ids |= set(profile)
    for tid in list(dislikes):
        if tid in current_ids:
            del dislikes[tid]
    return dislikes, false_negatives


def _first_line(err: Exception) -> str:
    text = str(err).splitlines()
    return (text[0] if text else repr(err))[:120]


async def act_on_picks(client: Client, tweets: list, digest: Digest,
                       now_iso: str) -> tuple[int, int, int, dict]:
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
    # This run's successful endorsements, in own-actions entry shape (incl. acted_at), so run()
    # can fold them into own_actions.json and a later run can detect their undo.
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
                endorsed["reposts"][tid] = _endorsement_entry(tid, handle, uid, source, now_iso)
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
                endorsed["likes"][tid] = _endorsement_entry(tid, handle, uid, source, now_iso)
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


def _endorsement_entry(tid: str, handle: str, uid: str, source, now_iso: str) -> dict:
    """A repost/like the script just made, in own-actions entry shape, so it folds straight into
    own_actions.json keyed by id. acted_at carries this run's clock."""
    return {
        "id": tid,
        "handle": handle,
        "author_id": uid,
        "text": tweet_text(source),
        "created_at": getattr(source, "created_at", None),
        "url": tweet_url(source),
        "acted_at": now_iso,
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
        sys.exit(f"opencode returned no JSON, got: {text.strip()[:300]}")
    return text[start:end + 1]


def render_positive_block(profile_reposts: dict | None, profile_likes: dict | None,
                          false_negatives: dict | None) -> str:
    """The user's in-window reposts/likes as positive calibration, or "" when there are none.

    Order matters: the manual picks (false negatives -- tweets the user reposted/liked that the
    SCRIPT did not) are rendered FIRST, so the REPOST_EXAMPLES cap can never drop these strongest
    want-signals; then the baseline kept endorsements (reposts before likes, the stronger signal
    first). Each manual pick is tagged "(added by you)". Capped to REPOST_EXAMPLES at render time
    only.
    """
    profile_reposts = profile_reposts or {}
    profile_likes = profile_likes or {}
    false_negatives = false_negatives or {}
    fn_reposts = false_negatives.get("reposts") or {}
    fn_likes = false_negatives.get("likes") or {}

    ordered: list[tuple[dict, bool]] = []
    # Manual picks first (reposts then likes) so the cap always keeps them.
    ordered += [(e, True) for e in fn_reposts.values()]
    ordered += [(e, True) for e in fn_likes.values()]
    # Then the baseline kept endorsements the script itself made (reposts first).
    ordered += [(e, False) for tid, e in profile_reposts.items() if tid not in fn_reposts]
    ordered += [(e, False) for tid, e in profile_likes.items() if tid not in fn_likes]
    if not ordered:
        return ""

    lines = []
    for entry, manual in ordered[:REPOST_EXAMPLES]:
        text = " ".join((entry.get("text") or "").split())
        if len(text) > 280:
            text = text[:280] + "..."
        tag = " (added by you)" if manual else ""
        lines.append(f"@{entry.get('handle', '?')}{tag}: {text}")
    body = "\n".join(lines)
    return (
        "\n<you_curated_these>\n"
        "These are tweets this person reposted or liked themselves -- ground truth of what they "
        "actually want. The ones tagged (added by you) they curated manually (the strongest "
        "want-signal). Treat them all as positive calibration: a candidate matching their "
        "substance and style should score high, and one resembling an (added by you) pick "
        "strongest of all. They do NOT override the hard technical-only filter or the persona "
        "above.\n\n"
        f"{body}\n"
        "</you_curated_these>\n"
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


def score_tweets(tweets: list, positive: tuple | None = None,
                 dislikes: dict[str, dict] | None = None) -> Digest:
    if shutil.which("opencode") is None:
        sys.exit("opencode CLI not found - install opencode first: https://opencode.ai")
    profile_reposts, profile_likes, false_negatives = positive or ({}, {}, {})
    payload = "\n\n---\n\n".join(render_for_model(i, t) for i, t in enumerate(tweets))
    schema = json.dumps(Digest.model_json_schema())
    prompt = (
        f"{SYSTEM}\n"
        f"{render_positive_block(profile_reposts, profile_likes, false_negatives)}\n"
        f"{render_dislikes_block(dislikes or {})}\n"
        f"Reply with one JSON object matching this schema. No prose, no code fences.\n"
        f"{schema}\n\n"
        f"The tweets:\n\n{payload}"
    )
    result = subprocess.run(
        ["opencode", "run", "--model", MODEL, "--format", "default", prompt],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        sys.exit(f"opencode run failed: {(result.stderr or result.stdout).strip()[:500]}")
    try:
        return Digest.model_validate_json(extract_json(result.stdout))
    except ValidationError as err:
        sys.exit(f"opencode returned JSON that does not match the schema: {err}")


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

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Windowed snapshot of what is actually on the profile right now (None on error/incomplete
    # coverage -- in which case that type is skipped both ways and its old file is kept).
    print("Mirroring your in-window reposts and likes from your profile...")
    fetched_reposts = await fetch_my_reposts(client)
    fetched_likes = await fetch_my_likes(client)
    profile_reposts = {r["id"]: r for r in fetched_reposts} if fetched_reposts is not None else None
    profile_likes = {r["id"]: r for r in fetched_likes} if fetched_likes is not None else None
    if profile_reposts is not None:
        print(f"  reposts: {len(profile_reposts)} in-window repost(s) currently visible")
    else:
        print("  reposts fetch failed/incomplete; keeping the existing reposts.json")
    if profile_likes is not None:
        print(f"  likes: {len(profile_likes)} in-window like(s) currently visible")
    else:
        print("  likes fetch failed/incomplete; keeping the existing likes.json")

    # Reconcile the script's own actions against the live profile: undos -> false positives
    # (dislikes), manual additions -> false negatives. Backfill first so the script's historical
    # picks (from actions.json) are not misread as manual additions.
    reposted, liked, _ = load_state()
    own = prune_own_actions(load_own_actions(), now)
    own = backfill_own_actions(own, reposted, liked, profile_reposts, profile_likes)
    dislikes, false_negatives = reconcile(
        own, profile_reposts, profile_likes, load_dislikes(), now_iso)
    n_fp = len(dislikes)
    n_fn = len(false_negatives["reposts"]) + len(false_negatives["likes"])
    print(f"  {n_fp} false positive(s) (undone) on the blocklist; "
          f"{n_fn} false negative(s) (your manual picks) surfaced")

    save_dislikes(dislikes)
    save_own_actions(own)
    if fetched_reposts is not None:
        save_reposts(fetched_reposts)  # rebuild reposts.json to exactly what is visible now
    if fetched_likes is not None:
        save_likes(fetched_likes)

    print(f"Fetching timeline (last {HOURS_BACK:g}h, max {MAX_TWEETS} tweets)...")
    tweets = await fetch_timeline(client)
    if not tweets:
        return None
    extras = []
    if profile_reposts or profile_likes:
        extras.append(f"{len(profile_reposts or {})} reposts / {len(profile_likes or {})} likes")
    if n_fn:
        extras.append(f"{n_fn} manual picks")
    if dislikes:
        extras.append(f"{len(dislikes)} false-positives")
    print(f"Scoring {len(tweets)} tweets against persona.md"
          f"{' + ' + ' / '.join(extras) if extras else ''}...")
    digest = score_tweets(
        tweets, (profile_reposts or {}, profile_likes or {}, false_negatives), dislikes)
    print(f"Reposting {REPOST_MIN_SCORE}+, liking {LIKE_MIN_SCORE}+, following {FOLLOW_MIN_SCORE}+ ...")
    n_re, n_li, n_fo, endorsed = await act_on_picks(client, tweets, digest, now_iso)

    # Fold this run's endorsements into own_actions so a later run can detect their undo.
    own["reposts"].update(endorsed["reposts"])
    own["likes"].update(endorsed["likes"])
    save_own_actions(own)

    return write_digest(tweets, digest, n_re, n_li, n_fo)


def main() -> None:
    load_dotenv(ROOT / ".env")
    path = asyncio.run(run())
    if path is None:
        sys.exit("No tweets found in the window; nothing to digest.")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
