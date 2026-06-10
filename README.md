# x-digest

Turns your X timeline into a self-curated feed. It reads the last 24 hours of your
"Following" timeline, scores every tweet against [persona.md](persona.md) with Claude, then
**reposts** the high-confidence picks (and **likes** the very best) so you can read your own
reposts instead of the firehose. It also writes a markdown digest to `digests/` as a log of
what it did and why. Run it once a day.

## How it works

1. [twikit](https://github.com/d60/twikit) reads your timeline using a browser session
   (the official API no longer has a free read tier; the cheapest plan that can read your
   timeline is $200/month). You log in to X once in your browser and hand the script two
   session cookies; it caches them in `cookies.json` and reuses the session after that.
2. Tweets get scored by Claude through the [Claude Code](https://code.claude.com) CLI
   (`claude -p`), with the persona as the rubric. It runs on whatever Claude login you
   already have, so there is no API key to manage. The model picks what is worth your time,
   scores it 1-10, and groups it by theme.
3. The script acts on the best picks, highest score first:
   - score **10**: reposted, liked, **and its author followed**
   - score **9**: reposted and liked
   - score **8**: reposted
   - everything else: left alone (but still listed in the digest down to `MIN_SCORE`)

   It works top-down (10s, then 9s, then 8s) and stops reposting once it hits the per-run
   cap, so the cap only ever drops the lowest-scoring picks. It never acts on the same thing
   twice: tweet ids it has reposted or liked and account ids it has followed are remembered
   in `actions.json`, and it also skips anything you reposted, liked, or follow yourself. New
   follows come mostly from reposts in your feed of accounts you don't already follow (when
   someone you follow boosts a perfect-score tweet from a new account).
4. Picks scoring `MIN_SCORE` (6) or higher are written to `digests/YYYY-MM-DD.md`, highest
   first, tagged with what action was taken. A "Skipped" section lists what dominated the
   feed but got filtered, so you can tell when the persona needs adjusting.

## Setup

Needs Python 3.10+ and [Claude Code](https://code.claude.com) installed and logged in
(`claude` must be on your PATH).

```sh
git clone https://github.com/aminmarashi/x-digest.git
cd x-digest
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

`.env` is gitignored.

**Get the two cookies.** Log in to X in your browser; this is where your passkey works.
Then open DevTools (F12) > Application > Cookies > `https://x.com` and copy the values of
`auth_token` and `ct0` into `.env`:

| Variable       | What                                            |
|----------------|--------------------------------------------------|
| `X_AUTH_TOKEN` | the `auth_token` cookie (your session token)     |
| `X_CT0`        | the `ct0` cookie (CSRF token)                     |

That is the whole login. The script writes `cookies.json` on the first run and reuses it,
so you only re-copy the cookies when the session eventually expires.

**Why not a password or passkey directly?** X passkey login is WebAuthn, which only works
in a browser with an authenticator, so a script can't perform it. The cookie approach is the
way to get a passkey-backed session into the script: you authenticate in the browser with
the passkey, then reuse the resulting session. (X also fronts its username/password login
with Cloudflare and frequently blocks it from scripts, so cookies are the only path here.)

## Run

```sh
python digest.py
```

It reposts/likes as it goes and writes `digests/<today>.md`.

Optional knobs, also via `.env` or the environment:

| Variable             | Default | What                                              |
|----------------------|---------|---------------------------------------------------|
| `CLAUDE_MODEL`       | `haiku` | model passed to `claude -p --model`               |
| `HOURS_BACK`         | `24`    | how far back to read the timeline                 |
| `MAX_TWEETS`         | `150`   | cap on tweets sent to the model                   |
| `MIN_SCORE`          | `6`     | minimum score to appear in the markdown digest    |
| `LIKE_MIN_SCORE`     | `8`     | liked at or above this score                      |
| `REPOST_MIN_SCORE`   | `9`     | reposted to your timeline at or above this score  |
| `FOLLOW_MIN_SCORE`   | `10`    | author followed at or above this score            |
| `REPOST_MAX_PER_RUN` | `25`    | safety cap on reposts per run                     |
| `FOLLOW_MAX_PER_RUN` | `10`    | safety cap on new follows per run                 |

## Make it yours

Everything the model knows about you lives in `persona.md`. Rewrite it: who you are, what to
surface, what to drop. It includes a hard "technical only" filter that drops non-technical
posts (policy complaints, AI hype/doom, punditry) even when the topic matches; loosen or
tighten that section to taste. The "Skipped" section at the bottom of each digest is the
feedback loop; if it keeps skipping things you wanted, adjust the persona. If too much (or
too little) is reaching your timeline, move `REPOST_MIN_SCORE` / `LIKE_MIN_SCORE` /
`FOLLOW_MIN_SCORE`.

## Caveats

- Reposts are public: they broadcast to your followers and show on your profile. That is the
  point here (you read your own reposts), but tune the thresholds so your stream stays clean.
- `twikit_patch.py` works around 2026 changes to X's web client that break twikit 2.3.3:
  its transaction-ID parsing ([d60/twikit#408](https://github.com/d60/twikit/issues/408))
  and its user-payload parsing (X dropped fields that twikit reads unconditionally). It is
  imported automatically; remove it once upstream ships fixes.
- Reading and posting through an unofficial client is against X's terms of service. One run
  a day is low traffic, but the account could in principle get flagged. Use a throwaway
  account if that worries you.
- Nothing leaves your machine except to go to X and to Claude Code. The session lives in the
  gitignored `cookies.json`; the repost/like history in the gitignored `actions.json`.
