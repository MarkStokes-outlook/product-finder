"""Rule-based condition grading from listing text."""

from __future__ import annotations

import re

GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"
SPARES = "spares/repair"
UNKNOWN = "unknown"

# Checked in order: spares first (a "like new, faulty" listing is still faulty),
# then C (heavy wear beats generic "used"), then A, then B.
_SPARES_TERMS = [
    "spares or repairs",
    "spares or repair",
    "spares/repairs",
    "spares & repairs",
    "for spares",
    "for parts",
    "parts only",
    "faulty",
    "not working",
    "doesn't work",
    "does not work",
    "broken",
    "untested",
    "no power",
    "won't turn on",
    "wont turn on",
    "missing motor",
    "needs repair",
    "sold as seen",
]
_C_TERMS = [
    "worn",
    "heavy use",
    "heavily used",
    "well used",
    "tatty",
    "rough",
    "scruffy",
    "damaged case",
    "cosmetic damage",
    "battle scars",
]
_A_TERMS = [
    "brand new",
    "new",
    "unused",
    "never used",
    "boxed",
    "sealed",
    "excellent condition",
    "immaculate",
    "mint",
    "as new",
    "like new",
    "barely used",
    "hardly used",
    "lightly used",
]
_B_TERMS = [
    "good condition",
    "great condition",
    "very good condition",
    "good working order",
    "fully working",
    "working",
    "tested",
    "used",
]


def _matches_any(text: str, terms: list[str]) -> bool:
    for term in terms:
        # Word-boundary match so "new" doesn't hit "Newcastle" or "renewed".
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text):
            return True
    return False


def classify(text: str) -> str:
    """Classify listing text into a condition grade."""
    text = (text or "").lower()
    if not text.strip():
        return UNKNOWN
    if _matches_any(text, _SPARES_TERMS):
        return SPARES
    if _matches_any(text, _C_TERMS):
        return GRADE_C
    if _matches_any(text, _A_TERMS):
        return GRADE_A
    if _matches_any(text, _B_TERMS):
        return GRADE_B
    return UNKNOWN
