# x-digest

Turns your X timeline into a short daily reading list. It fetches the last 24 hours of your
"Following" feed, scores every tweet against [persona.md](persona.md) with Claude, and writes
a markdown digest grouped by theme to `digests/`. Run it once a day; read the output instead
of the timeline.

## How it works

1. [twikit](https://github.com/d60/twikit) logs in to X with your username and password
   (the official API no longer has a free read tier; the cheapest plan that can read your
   timeline is $200/month). Credentials are read from `.env` as 1Password secret
   references and resolved at runtime with the `op` CLI, so no secret is stored on disk.
   The session cookie is cached in `cookies.json`, so it logs in once and reuses the
   session after that.
2. Tweets get scored by Claude through the [Claude Code](https://code.claude.com) CLI
   (`claude -p`), with the persona as the rubric. It runs on whatever Claude login you
   already have, so there is no API key to manage. The model picks what is worth your time,
   scores it 1-10, and groups it by theme.
3. Picks scoring 6 or higher land in `digests/YYYY-MM-DD.md`, highest score first, with the
   tweet link and any external links. A "Skipped" section lists what dominated the feed but
   got filtered, so you can tell when the persona needs adjusting.

## Setup

Needs Python 3.10+, [Claude Code](https://code.claude.com) installed and logged in
(`claude` must be on your PATH), and the
[1Password CLI](https://developer.1password.com/docs/cli/get-started/) with the desktop
app integration enabled (Settings > Developer > "Integrate with 1Password CLI") if you
keep the credentials in 1Password.

```sh
git clone https://github.com/aminmarashi/x-digest.git
cd x-digest
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

`.env` is gitignored, and with 1Password references it contains no secrets at all: any
value of the form `op://<vault>/<item>/<field>` is resolved through `op read` when the
script runs, so the password only ever exists in your vault and in memory. Plain values
still work if you skip 1Password.

| Variable        | What                                                          |
|-----------------|---------------------------------------------------------------|
| `X_USERNAME`    | your X handle, without the @                                  |
| `X_EMAIL`       | the email on the account (X sometimes asks for it at login)   |
| `X_PASSWORD`    | your X password                                               |
| `X_TOTP_SECRET` | optional, the base32 TOTP secret if the account has 2FA       |

A note on passkeys: X passkey login is WebAuthn, which only works through a browser with
an authenticator (like the 1Password extension). twikit drives the app login flow, so it
still needs the password; storing it in 1Password and resolving it at runtime is as close
as this setup can get.

## Run

```sh
python digest.py
```

Then open `digests/<today>.md`.

Optional knobs, also via `.env` or the environment:

| Variable       | Default | What                                       |
|----------------|---------|--------------------------------------------|
| `CLAUDE_MODEL` | `haiku` | model passed to `claude -p --model`        |
| `HOURS_BACK`   | `24`    | how far back to read the timeline          |
| `MAX_TWEETS`   | `150`   | cap on tweets sent to the model            |
| `MIN_SCORE`    | `6`     | minimum relevance score to make the digest |

## Make it yours

Everything the model knows about you lives in `persona.md`. Rewrite it: who you are, what to
surface, what to drop. The "Skipped" section at the bottom of each digest is the feedback
loop; if it keeps skipping things you wanted, add them to the persona.

## Caveats

- Logging in with credentials through an unofficial client is against X's terms of service.
  One read-only run a day is low traffic, but the account could in principle get flagged.
  Use a throwaway account if that worries you.
- Credentials never leave your machine except to go to X itself; they live in 1Password
  (or the gitignored `.env`), and the cached session in the gitignored `cookies.json`.
