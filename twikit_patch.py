"""Patch for twikit 2.3.3 against X's March 2026 webpack change (d60/twikit#408).

X now lists the ondemand.s script in a webpack chunk map (`,<id>:"ondemand.s"`) with the
file hash in a separate `<id>:"<hash>"` entry, and the script's parseInt calls changed
shape. twikit's regexes find neither, so every request dies with "Couldn't get KEY_BYTE
indices". This module replaces ClientTransaction.get_indices with a version that tries
the legacy patterns first and falls back to the new format. Importing it applies the
patch. Delete once upstream ships a fix (open PRs: #410, #411, #416).
"""

import re

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
