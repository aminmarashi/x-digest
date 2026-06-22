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

3. Retweet query IDs: X rotated the persisted-query operation IDs, so twikit's hardcoded
   CreateRetweet/DeleteRetweet endpoints now 404 (empty body -> NotFound) while likes and
   follows still work. The GQLClient.retweet wrapper re-discovers the live IDs from X's web
   bundle on a 404, caches them in query_ids.json, and retries once. Cached IDs are applied
   on import so the rediscovery only ever runs the first time after a rotation.

4. Error-code extraction: when X returns an error envelope, Client.request reads the
   code with a hard `response_data['errors'][0]['code']`. X now nests the code GraphQL-style
   under `errors[0].extensions.code` with no top-level `code`, so the subscript raises
   `KeyError: code` and masks the real error -- which is why both the timeline and reposts
   fetches died with the bare message "code". request is replaced with a version that reads
   the code defensively (`error.get('code')`, then `error.get('extensions', {}).get('code')`)
   so a missing code falls through to the normal HTTP status handling and the real X error
   surfaces. twikit's own errors.raise_exceptions_from_response already does this; request
   was never updated.

Delete once upstream ships fixes (open PRs: #410, #411, #416). The retweet patch
(section 3) self-heals, so it can stay until upstream tracks the rotating IDs.
"""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from twikit import user as user_module
from twikit.client import gql
from twikit.client.client import DOMAIN, Client
from twikit.client.gql import Endpoint, GQLClient
from twikit.errors import (
    AccountLocked,
    AccountSuspended,
    BadRequest,
    Forbidden,
    NotFound,
    RequestTimeout,
    ServerError,
    TooManyRequests,
    TwitterException,
    Unauthorized,
)
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


# --- 3. Self-healing retweet query IDs -------------------------------------------------

# X serves CreateRetweet/DeleteRetweet as persisted GraphQL queries keyed by an operation
# ID baked into the web bundle. When X rotates an ID, twikit's hardcoded endpoint 404s
# (empty body -> NotFound) even though the tweet exists -- which is why reposts broke while
# likes/follows kept working. We cache the live IDs next to cookies.json and, on a 404,
# re-scrape them from the current web bundle and retry once. Only these two ops are touched.

QUERY_IDS_FILE = Path(__file__).parent / "query_ids.json"

# operation name -> the Endpoint attribute that carries its URL
RETWEET_OPS = {"CreateRetweet": "CREATE_RETWEET", "DeleteRetweet": "DELETE_RETWEET"}

# abs.twimg.com client-web bundle URLs, e.g. .../client-web/main.<hash>.js
BUNDLE_URL_REGEX = re.compile(
    r'https://abs\.twimg\.com/responsive-web/client-web[^"\']*?\.js')
# Operation ID charset; 16+ chars covers the 22-char base64-ish IDs X currently uses.
QUERY_ID_REGEX = re.compile(r'queryId:"([A-Za-z0-9_-]{16,})"')

# Browser UA so abs.twimg.com / x.com serve the real web bundle rather than a stub.
DISCOVERY_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _endpoint_url(query_id: str, operation: str) -> str:
    """Rebuild an Endpoint URL the same way twikit's Endpoint.url() does."""
    return f"https://{gql.DOMAIN}/i/api/graphql/{query_id}/{operation}"


def _current_query_id(operation: str) -> str | None:
    url = getattr(Endpoint, RETWEET_OPS[operation], None)
    return gql.get_query_id(url) if url else None


def _apply_query_ids(ids: dict) -> None:
    """Point the relevant Endpoint attributes at the given operation IDs."""
    for operation, attr in RETWEET_OPS.items():
        query_id = ids.get(operation)
        if query_id:
            setattr(Endpoint, attr, _endpoint_url(query_id, operation))


def _load_cached_query_ids() -> dict:
    try:
        data = json.loads(QUERY_IDS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {op: data[op] for op in RETWEET_OPS if isinstance(data.get(op), str)}


def _persist_query_ids(ids: dict) -> None:
    merged = _load_cached_query_ids()
    merged.update({op: v for op, v in ids.items() if op in RETWEET_OPS and v})
    try:
        QUERY_IDS_FILE.write_text(json.dumps(merged, indent=2))
    except OSError:
        pass  # cache is best-effort; a write failure just means we rediscover next time


def _extract_query_id(script: str, operation: str) -> str | None:
    """Find an operation's queryId in a minified bundle, tolerant of field order.

    The live shape is `queryId:"<id>",operationName:"<op>"`, but X minifies field order
    freely. Within one persisted-query module object queryId and operationName sit right
    next to each other, so we anchor on the operation name and pick the *closest* queryId
    in either direction -- a queryId belonging to a neighbouring module is much farther off.
    """
    op_matches = list(re.finditer(rf'operationName:"{re.escape(operation)}"', script))
    if not op_matches:
        return None
    qid_matches = list(QUERY_ID_REGEX.finditer(script))
    best_id, best_dist = None, None
    for op_match in op_matches:
        for qid_match in qid_matches:
            dist = abs(qid_match.start() - op_match.start())
            if dist <= 400 and (best_dist is None or dist < best_dist):
                best_id, best_dist = qid_match.group(1), dist
    return best_id


async def _fetch_text(session, url: str) -> str:
    response = await session.request(
        method="GET", url=url, headers={"User-Agent": DISCOVERY_USER_AGENT})
    return str(response.text)


async def discover_query_ids(session, user_agent: str | None = None,
                             home_html: str | None = None) -> dict:
    """Best-effort scrape of the current CreateRetweet/DeleteRetweet IDs from X's web bundle.

    Fetches x.com's home page (following the migration redirect), enumerates the
    client-web JS bundles it references, and regexes each for the operation IDs. Any
    network or parse failure returns {} so the caller keeps twikit's fallback ID.
    Returns only the operations actually found.
    """
    try:
        if home_html is None:
            headers = {
                "User-Agent": user_agent or DISCOVERY_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            }
            home_html = str(await transaction.handle_x_migration(session, headers))

        found: dict = {}
        bundle_urls = list(dict.fromkeys(BUNDLE_URL_REGEX.findall(home_html)))
        # main.<hash>.js currently carries the mutations, so try it first.
        bundle_urls.sort(key=lambda u: 0 if "/main." in u else 1)

        for url in bundle_urls:
            script = await _fetch_text(session, url)
            for operation in RETWEET_OPS:
                if operation not in found:
                    query_id = _extract_query_id(script, operation)
                    if query_id:
                        found[operation] = query_id
            if all(op in found for op in RETWEET_OPS):
                break
        return found
    except Exception:
        return {}


_original_retweet = GQLClient.retweet


async def patched_retweet(self, tweet_id):
    try:
        return await _original_retweet(self, tweet_id)
    except NotFound:
        # The ID may have rotated. Re-scrape the live bundle using the already-authed
        # session. handle_x_migration may have stashed the home page on this client.
        ct = getattr(self.base, "client_transaction", None)
        home_response = getattr(ct, "home_page_response", None)
        home_html = str(home_response) if home_response else None
        discovered = await discover_query_ids(
            self.base.http, getattr(self.base, "_user_agent", None), home_html=home_html)

        new_create = discovered.get("CreateRetweet")
        if not new_create or new_create == _current_query_id("CreateRetweet"):
            raise  # discovery found nothing new -> behave exactly like stock twikit

        _apply_query_ids(discovered)
        _persist_query_ids(discovered)
        return await _original_retweet(self, tweet_id)


GQLClient.retweet = patched_retweet

# Apply cached IDs on import so a known rotation costs nothing on subsequent runs.
_apply_query_ids(_load_cached_query_ids())


# --- 4. Defensive error-code extraction in Client.request -----------------------------

# When X returns an error envelope, stock request does `response_data['errors'][0]['code']`.
# X now nests the code under `errors[0].extensions.code` (GraphQL style) with no top-level
# `code`, so the subscript raises `KeyError: code` and masks the real error -- the cause of
# both the timeline and reposts fetches failing with the bare message "code". We replace
# request with a copy that reads the code defensively (top-level first, then extensions);
# a missing code stays None, skips the suspended/locked special-casing, and falls through
# to the normal HTTP status handling so the real X error (status + message) surfaces.


async def patched_request(
    self,
    method: str,
    url: str,
    auto_unlock: bool = True,
    raise_exception: bool = True,
    **kwargs
):
    ':meta private:'
    headers = kwargs.pop('headers', {})

    if not self.client_transaction.home_page_response:
        cookies_backup = self.get_cookies().copy()
        ct_headers = {
            'Accept-Language': f'{self.language},{self.language.split("-")[0]};q=0.9',
            'Cache-Control': 'no-cache',
            'Referer': f'https://{DOMAIN}',
            'User-Agent': self._user_agent
        }
        await self.client_transaction.init(self.http, ct_headers)
        self.set_cookies(cookies_backup, clear_cookies=True)

    tid = self.client_transaction.generate_transaction_id(method=method, path=urlparse(url).path)
    headers['X-Client-Transaction-Id'] = tid

    cookies_backup = self.get_cookies().copy()
    response = await self.http.request(method, url, headers=headers, **kwargs)
    self._remove_duplicate_ct0_cookie()

    try:
        response_data = response.json()
    except json.decoder.JSONDecodeError:
        response_data = response.text

    if isinstance(response_data, dict) and 'errors' in response_data:
        error = response_data['errors'][0]
        # X nests the code GraphQL-style under extensions; tolerate a missing top-level code.
        error_code = error.get('code')
        if error_code is None:
            error_code = error.get('extensions', {}).get('code')
        error_message = error.get('message')
        if error_code in (37, 64):
            # Account suspended
            raise AccountSuspended(error_message)

        if error_code == 326:
            # Account unlocking
            if self.captcha_solver is None:
                raise AccountLocked(
                    'Your account is locked. Visit '
                    f'https://{DOMAIN}/account/access to unlock it.'
                )
            if auto_unlock:
                await self.unlock()
                self.set_cookies(cookies_backup, clear_cookies=True)
                response = await self.http.request(method, url, **kwargs)
                self._remove_duplicate_ct0_cookie()
                try:
                    response_data = response.json()
                except json.decoder.JSONDecodeError:
                    response_data = response.text

    status_code = response.status_code

    if status_code >= 400 and raise_exception:
        message = f'status: {status_code}, message: "{response.text}"'
        if status_code == 400:
            raise BadRequest(message, headers=response.headers)
        elif status_code == 401:
            raise Unauthorized(message, headers=response.headers)
        elif status_code == 403:
            raise Forbidden(message, headers=response.headers)
        elif status_code == 404:
            raise NotFound(message, headers=response.headers)
        elif status_code == 408:
            raise RequestTimeout(message, headers=response.headers)
        elif status_code == 429:
            if await self._get_user_state() == 'suspended':
                raise AccountSuspended(message, headers=response.headers)
            raise TooManyRequests(message, headers=response.headers)
        elif 500 <= status_code < 600:
            raise ServerError(message, headers=response.headers)
        else:
            raise TwitterException(message, headers=response.headers)

    return response_data, response


Client.request = patched_request
