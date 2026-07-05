# Implementation notes — suggestion triage (evidence-based verdict buckets)

**Date:** 2026-07-05
**Scope:** `db.py` (`triage_pending_suggestions` + thresholds,
`list_all_pending_suggestions` gains item_normal), `web/app.py`
(catalogue route), `catalogue.html` (verdict-grouped rewrite of the
suggestions section), `tests/test_catalogue_tidy.py` (+6)
**Status:** shipped, 406 tests passing. Trigger: operator opened the queue
— "holy crap, there's a lot of products for me to check and approve."

## Design

Applies the evidence-gating direction to the *pending* queue, before
approval, using the same signals that exposed bad products after approval
(suspect products): for each suggestion, evidence = the item's own matched
listings whose titles mention the suggested model (word-boundary, so model
"774" takes nothing from "DWS774" titles).

- ≥2 evidence listings required for any verdict (no evidence, no verdict —
  consistent with find_suspect_products).
- `strong` — avg evidence price ≥40% of item normal AND ≤25% accessory-
  worded → approve as a bucket.
- `accessory` — ≥50% accessory-worded OR avg price <25% of item normal →
  dismiss as a bucket.
- `unclear` — mixed signals or no evidence yet.
- `brand-only` — no model (already excluded from bulk approve).

Verdicts are proposals; `/catalogue` groups by verdict with one select-all
per bucket and per-row evidence prose. Nothing auto-approves — the
existing confidence-based auto_approve_threshold is untouched (and still
evidence-blind; upgrading it to require a `strong` verdict is the obvious
next step once the operator trusts the buckets).

## Live-queue result (2026-07-05, 1,621 pending, 0.6s to compute)

- **48 strong** — spot-checked: real dust extractors (Festool 578329,
  Makita VC1310L) and motherboards priced like the items.
- **127 accessory** — caught Festool 204308 dust bags *pre-approval* this
  time, Makita P-72899 bags, Titan 1L/5L paint containers.
- **1,043 unclear** — overwhelmingly "no listings mention this model yet":
  suggestions born from `get_item_details()` structured fields where the
  listing title doesn't contain the model string. These resolve naturally
  as more listings accumulate; they cost nothing while pending.
- **403 brand-only.**

## Honest caveats (told to the operator)

- The accessory bucket can contain class-2 cases: a real product whose
  only mentions are accessory listings ("bags for VC2012L" — VC2012L is a
  real vacuum; Leica NA724 is a real optical level). Bucket-dismissing is
  a skim-then-tick action, not blind. The proper fix remains the
  listing-level accessory gate.
- Strong ≠ verified: it means "priced like the item, not accessory-worded".

## Ordering note

Triage runs at page load (0.6s on 1,621 pending / ~5k listings). If the
queue or listing volume grows 10×, precompute during the watch cycle
instead — deliberately not built yet.
