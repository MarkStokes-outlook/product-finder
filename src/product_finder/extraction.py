"""Ollama-based free-text brand/model extraction — fallback for listings
with no structured eBay brand/mpn (private/casual sellers routinely skip
those fields; see `sources/ebay.py:get_item_details()`).

`extract_brand_model()` is the only entry point and is deliberately narrow:
text in, a `{"brand", "model"}` candidate or `None` out. It knows nothing
about the database or the suggestion queue — the caller (runner.py) feeds
its result through the exact same `db.record_suggestion_sighting()` path
already used for structured suggestions, so an LLM guess gets the same
human-review gate, never a direct write to the catalogue.
"""

from __future__ import annotations

import json
import logging

import requests

from .config import OllamaConfig

log = logging.getLogger(__name__)

_PROMPT = """You extract the manufacturer brand and model number of a single \
physical product from a marketplace listing's title and description.

Respond with JSON only, no other text: \
{{"brand": string, "model": string, "confidence": number between 0 and 1}}

If you cannot confidently identify a specific manufacturer, respond with \
{{"brand": "", "model": "", "confidence": 0}}. Never invent a brand or model \
that isn't stated or clearly implied in the text.

Listing text:
{text}
"""


def extract_brand_model(text: str, cfg: OllamaConfig) -> dict | None:
    """Best-effort (brand, model) extraction from listing text via a local
    Ollama model. Returns None — never raises — if extraction is disabled,
    Ollama is unreachable, the response is malformed, no brand was found, or
    the model's self-reported confidence is below `cfg.minimum_confidence`.
    Every skip reason is logged so a quiet Ollama outage is visible, not
    silent."""
    if not cfg.enabled:
        return None

    try:
        resp = requests.post(
            f"{cfg.base_url.rstrip('/')}/api/generate",
            json={
                "model": cfg.model,
                "prompt": _PROMPT.format(text=text),
                "format": "json",
                "stream": False,
            },
            timeout=cfg.timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Ollama extraction skipped (unavailable): %s", exc)
        return None

    try:
        parsed = json.loads(resp.json()["response"])
        brand = str(parsed.get("brand") or "").strip()
        model = str(parsed.get("model") or "").strip()
        confidence = float(parsed.get("confidence"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        log.warning("Ollama extraction skipped (malformed response): %s", exc)
        return None

    if not brand:
        return None
    if confidence < cfg.minimum_confidence:
        log.info(
            "Ollama extraction skipped (confidence %.2f below minimum %.2f)",
            confidence, cfg.minimum_confidence,
        )
        return None

    return {"brand": brand, "model": model}
