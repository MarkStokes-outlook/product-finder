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


# A term match immediately preceded by a negator ("no dead pixels", "not
# scratched") is the listing *ruling out* a fault, not reporting one — the
# opposite of a bare keyword hit. Deliberately small and literal, same style
# as the term lists above: an unlisted negator phrase just goes unrecognised,
# never misapplied. "free" is deliberately excluded — "free delivery"/"free
# postage" are common enough in UK listings that treating it as a negator
# would suppress real faults mentioned nearby (e.g. "free P&P, scratched").
_NEGATORS = {"no", "not", "never", "without"}
_NEGATION_WINDOW = 3  # words of context scanned before a match

# Negation is scoped to the current clause (stops at . ! ? \n or a comma) so
# it can't leak across distinct facts — e.g. "no charger, no battery
# included" must flag the missing battery on its own merits, and "No
# accessories included. Faulty motor." must still flag the fault in the next
# sentence. This trades away one thing: a fault genuinely covered by an
# earlier negator across a comma-joined list (e.g. "no scratches, dents or
# cracks" only suppresses "scratches" — "dents"/"cracks" would need their own
# "no") goes uncaught. That's a deliberately accepted false-negative — safer
# than the alternative of a negator bleeding into an unrelated later clause
# and hiding a real fault, which is what a comma-blind window did before.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?\n,]")


def _is_negated(text: str, match_start: int) -> bool:
    before = text[:match_start]
    boundary = max((m.end() for m in _SENTENCE_BOUNDARY_RE.finditer(before)), default=0)
    words = re.findall(r"[\w']+", before[boundary:])[-_NEGATION_WINDOW:]
    return any(w in _NEGATORS for w in words)


def phrase_present(text: str, phrase: str) -> bool:
    """True if `phrase` appears in `text` as a whole-word/phrase match that
    isn't immediately negated. The single shared primitive behind condition
    classification here and scoring.warning_flags() — kept in one place so
    the negation rules can't drift between the two."""
    # Word-boundary match so "new" doesn't hit "Newcastle" or "renewed".
    for match in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
        if not _is_negated(text, match.start()):
            return True
    return False


def _matches_any(text: str, terms: list[str]) -> bool:
    return any(phrase_present(text, term) for term in terms)


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
