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

ROOT = Path(__file__).parent
COOKIES_FILE = ROOT / "cookies.json"
DIGEST_DIR = ROOT / "digests"
PERSONA = (ROOT / "persona.md").read_text()

MODEL = os.getenv("CLAUDE_MODEL", "haiku")
MAX_TWEETS = int(os.getenv("MAX_TWEETS", "150"))
HOURS_BACK = float(os.getenv("HOURS_BACK", "24"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))

SYSTEM = f"""You curate a daily reading list from an X timeline for one specific person.
Their persona follows; treat it as the rubric.

<persona>
{PERSONA}
</persona>

You get a numbered list of tweets. For each tweet worth this person's reading time, emit a
pick with the tweet's index, a short theme (2-4 words, reuse themes across picks so they
group well), a 1-10 relevance score, and one sentence on why it earns a spot. Score 10 means
"would have hunted this down anyway", 6 means "worth a skim". Skip everything below 6; do
not pad the list. Judge linked content by the tweet's description of it. Retweets count for
their content, not the retweeter. Also report skipped_themes: the 3-5 topics that dominated
the timeline but did not make the cut, so the person can sanity-check the filter."""


class Pick(BaseModel):
    tweet_index: int
    theme: str
    score: int
    reason: str


class Digest(BaseModel):
    picks: list[Pick]
    skipped_themes: list[str]


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} - copy .env.example to .env and fill it in.")


def env_secret(name: str) -> str | None:
    """Read an env var; values like op://vault/item/field resolve via the 1Password CLI."""
    value = os.getenv(name)
    if not value or not value.startswith("op://"):
        return value
    if shutil.which("op") is None:
        sys.exit(f"{name} is a 1Password reference but the op CLI is not installed:\n"
                 "https://developer.1password.com/docs/cli/get-started/")
    result = subprocess.run(["op", "read", "--no-newline", value],
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"op read failed for {name}: {result.stderr.strip()[:300]}\n"
                 "Unlock 1Password first (enable the desktop app CLI integration, "
                 "or run: eval $(op signin)).")
    return result.stdout


async def fetch_tweets() -> list:
    username = env_secret("X_USERNAME")
    client = Client("en-US")
    await client.login(
        auth_info_1=username,
        auth_info_2=env_secret("X_EMAIL") or username,
        password=env_secret("X_PASSWORD"),
        totp_secret=env_secret("X_TOTP_SECRET"),
        cookies_file=str(COOKIES_FILE),
    )
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


def score_tweets(tweets: list) -> Digest:
    if shutil.which("claude") is None:
        sys.exit("claude CLI not found - install Claude Code first: https://code.claude.com")
    payload = "\n\n---\n\n".join(render_for_model(i, t) for i, t in enumerate(tweets))
    schema = json.dumps(Digest.model_json_schema())
    prompt = (
        f"{SYSTEM}\n\n"
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


def write_digest(tweets: list, digest: Digest) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    valid = [p for p in digest.picks if 0 <= p.tweet_index < len(tweets) and p.score >= MIN_SCORE]
    by_theme: dict[str, list[Pick]] = {}
    for pick in sorted(valid, key=lambda p: -p.score):
        by_theme.setdefault(pick.theme, []).append(pick)

    lines = [f"# Reading list, {today}", ""]
    lines.append(f"{len(valid)} picks from {len(tweets)} tweets in the last {HOURS_BACK:g} hours.")
    lines.append("")
    for theme, picks in sorted(by_theme.items(), key=lambda kv: -max(p.score for p in kv[1])):
        lines.append(f"## {theme}")
        lines.append("")
        for pick in picks:
            tweet = tweets[pick.tweet_index]
            source = getattr(tweet, "retweeted_tweet", None) or tweet
            snippet = " ".join(tweet_text(source).split())
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(f"- **@{source.user.screen_name}** ({pick.score}/10) {pick.reason}")
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


def main() -> None:
    load_dotenv(ROOT / ".env")
    require_env("X_USERNAME", "X_PASSWORD")

    print(f"Fetching timeline (last {HOURS_BACK:g}h, max {MAX_TWEETS} tweets)...")
    tweets = asyncio.run(fetch_tweets())
    if not tweets:
        sys.exit("No tweets found in the window; nothing to digest.")
    print(f"Scoring {len(tweets)} tweets against persona.md...")
    digest = score_tweets(tweets)
    path = write_digest(tweets, digest)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
