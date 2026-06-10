"""Patches for twikit 2.3.3 against X's 2026 web-client changes. Importing applies them all.

1. Transaction IDs (d60/twikit#408): X now lists the ondemand.s script in a webpack chunk
   map (`,<id>:"ondemand.s"`) with the file hash in a separate `<id>:"<hash>"` entry, and
   the script's parseInt calls changed shape. twikit's regexes find neither, so every
   request dies with "Couldn't get KEY_BYTE indices". get_indices is replaced with a
   version that tries the legacy patterns first and falls back to the new format.

2. User parsing: X trimmed fields from the user payload (e.g. entities.description.urls),
   but twikit's User.__init__ reads them with hard `legacy['key']` access and raises
   KeyError on any timeline that contains such a user. The wrapper backfills the keys
   twikit assumes with safe defaults before delegating to the original __init__.

Delete once upstream ships fixes (open PRs: #410, #411, #416).
"""

import re

from twikit import user as user_module
from twikit.x_client_transaction import transaction

CHUNK_ID_REGEX = re.compile(r'[,{]\s*(\d+)\s*:\s*["\']ondemand\.s["\']')
INDICES_REGEX = re.compile(r'\[\s*(\d{1,2})\s*\]\s*,\s*16\s*\)')


def find_file_hash(html: str) -> str | None:
    legacy = transaction.ON_DEMAND_FILE_REGEX.search(html)
    if legacy:
        return legacy.group(1)
    chunk = CHUNK_ID_REGEX.search(html)
    if not chunk:
        return None
    hash_match = re.search(
        r'[,{{]\s*{}\s*:\s*["\']([0-9a-f]{{6,}})["\']'.format(chunk.group(1)), html)
    return hash_match.group(1) if hash_match else None


async def get_indices(self, home_page_response, session, headers):
    response = self.validate_response(home_page_response) or self.home_page_response
    html = str(response)

    file_hash = find_file_hash(html)
    if not file_hash:
        raise Exception("Couldn't find the ondemand.s file hash on the X home page")

    base = "https://abs.twimg.com/responsive-web/client-web/ondemand.s"
    key_byte_indices = []
    for url in (f"{base}.{file_hash}a.js", f"{base}.{file_hash}.js"):
        script_response = await session.request(method="GET", url=url, headers=headers)
        script = str(script_response.text)
        key_byte_indices = [m.group(2) for m in transaction.INDICES_REGEX.finditer(script)]
        if not key_byte_indices:
            key_byte_indices = [m.group(1) for m in INDICES_REGEX.finditer(script)]
        if key_byte_indices:
            break
    if not key_byte_indices:
        raise Exception("Couldn't get KEY_BYTE indices")
    key_byte_indices = list(map(int, key_byte_indices))
    return key_byte_indices[0], key_byte_indices[1:]


transaction.ClientTransaction.get_indices = get_indices


# --- 2. Tolerate trimmed user payloads -------------------------------------------------

# Keys User.__init__ reads via legacy['key'] (hard access). X drops some over time.
LEGACY_DEFAULTS = {
    "created_at": None, "name": None, "screen_name": None,
    "profile_image_url_https": None, "location": None, "description": "",
    "pinned_tweet_ids_str": [], "verified": False, "possibly_sensitive": False,
    "can_dm": False, "can_media_tag": False, "want_retweets": False,
    "default_profile": False, "default_profile_image": False,
    "has_custom_timelines": False, "followers_count": 0, "fast_followers_count": 0,
    "normal_followers_count": 0, "friends_count": 0, "favourites_count": 0,
    "listed_count": 0, "media_count": 0, "statuses_count": 0,
    "is_translator": False, "translator_type": None, "withheld_in_countries": [],
}

_original_user_init = user_module.User.__init__


def patched_user_init(self, client, data):
    data.setdefault("is_blue_verified", False)
    legacy = data.setdefault("legacy", {})
    for key, default in LEGACY_DEFAULTS.items():
        legacy.setdefault(key, default)
    entities = legacy.setdefault("entities", {})
    entities.setdefault("description", {}).setdefault("urls", [])
    _original_user_init(self, client, data)
    # twikit doesn't surface this; expose whether the authed user already follows them
    # so we don't fire needless follow calls.
    self.following = legacy.get("following")


user_module.User.__init__ = patched_user_init
