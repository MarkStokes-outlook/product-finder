"""Cross-source listing identity — v1: canonical-URL matching only.

A generic source (e.g. an RSS feed) can end up pointing at a listing that's
already been seen through another source's own native ID — most concretely,
an RSS entry whose link is itself an eBay item page. This module recognises
that case by extracting a platform's own stable ID from a listing's URL, so
`db.resolve_identity()` can treat both sightings as one real-world listing.

Deliberately narrow: only ships a pattern where a platform's own ID is
recoverable straight from the URL. Fuzzy title/price matching across
marketplaces with no shared ID is explicitly out of scope for v1 — see
docs/strategy/roadmap.md, "Identity resolution".
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# (host suffix, path pattern). Path pattern's one capture group must be the
# platform's own stable numeric ID — never a slug, never a feed-specific GUID.
# A single entry today (eBay); adding another platform is one more row here.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ebay", re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")),
]

_HOST_PLATFORM = {
    "ebay": re.compile(r"(^|\.)ebay\.[a-z.]{2,}$", re.IGNORECASE),
}


def derive_canonical_key(url: str) -> str | None:
    """A stable cross-source identity key derived from a URL's own
    platform-native ID, or None if no known pattern matches. Host-gated
    (e.g. requires an `ebay.<tld>` host, not just an "/itm/" substring
    anywhere) so an unrelated URL can never be mistaken for a match."""
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path
    for platform, path_pattern in _PATTERNS:
        host_pattern = _HOST_PLATFORM[platform]
        if not host_pattern.search(host):
            continue
        match = path_pattern.search(path)
        if match:
            return f"{platform}:{match.group(1)}"
    return None
