# ADR-0001: Phased path to public/commercial readiness

**Status:** Proposed (revised 2026-07-08 — see Revision note)
**Date:** 2026-07-08
**Related backlog:** EPIC-100 (prerequisite, not a numbered phase), EPIC-101, EPIC-102, EPIC-103, EPIC-104, EPIC-105
**Related ADRs:** ADR-0002 .. ADR-0006 (one per phase), ADR-0007 (Catalogue globalization — prerequisite)

## Revision note (2026-07-08)

Sanity-checking Phase 3's ownership boundary (ADR-0004) surfaced a real,
pre-existing schema mismatch: `products` is scoped to a single `item_id`,
not global, contradicting the "catalogue is platform-owned/shared" product
decision. That work is specced separately in **ADR-0007 (Catalogue
globalization, EPIC-100)** rather than folded into Phase 3, and this ADR is
revised to record where it sits in the sequence.

## Context

Product Finder is currently a local, single-user Python/Flask/SQLite application
with no authentication, no accounts, and no concept of ownership (see
`docs/architecture-briefing.md`). `docs/strategy/roadmap.md` already names the
destination — affiliate-supported public discovery with signed-in ownership,
Authentik/OIDC for auth — as a future idea, but not an execution path.

Turning a local single-user tool into something with public reach and
commercial mechanics (affiliate revenue, accounts, sharing) touches
authentication, authorization, data ownership, and the public surface area
simultaneously if done carelessly. Doing it as one large change is high-risk
for a codebase whose stated design philosophy is "working software over
perfect software" and "small, maintainable codebase" (`docs/architecture-briefing.md`,
Design Philosophy).

## Decision

Deliver this as five sequential, **independently shippable** phases, plus
one prerequisite data-model fix that is not itself one of the five phases.
Each phase merges to `main` on its own, is useful (or at minimum
inert-and-safe) by itself, and does not require a later phase to be correct
or valuable:

0. **Catalogue globalization** (ADR-0007, EPIC-100) — decouple `products`
   from `items` so the catalogue is genuinely platform-owned/shared, as the
   product boundary requires. Not a numbered phase — a prerequisite data-model
   fix, sequenced before Phase 3 by recommendation and before Phases 4–5 as a
   hard blocking dependency. See "Prerequisite: Catalogue globalization" below.
1. **Affiliate links** (ADR-0002) — outbound redirect/tracking, no accounts involved.
2. **Authentik authentication backend** (ADR-0003) — login/logout/session plumbing, no ownership changes yet.
3. **User-owned data** (ADR-0004) — ownership + authorization + migration of existing data.
4. **Public homepage/search experience** (ADR-0005) — anonymous-safe public surface, gated actions.
5. **Project sharing, invites, and cloning** (ADR-0006) — the first cross-user collaboration primitive.

**Sequencing rationale:**

- Affiliate links first because they are valuable independently of accounts,
  and isolating them first means the highest-scrutiny, money-adjacent change
  (destination redirection, click tracking) gets its own audit trail and
  review before anything else changes.
- Auth backend before ownership because login/session correctness (can a
  user log in, stay logged in, log out) should be provable in isolation
  before authorization rules are layered on top — this reduces the blast
  radius of getting either one wrong, and matches the user's own guidance
  not to tie data ownership to Phase 2 unless the auth plumbing needs it.
- Ownership before the public split because gating "what anonymous users
  cannot do" requires an authorization model to already exist to gate against.
- Public split before sharing because sharing is meaningless before there is
  a public/private boundary and more than one real account can plausibly exist.

## Prerequisite: Catalogue globalization (ADR-0007, EPIC-100)

Catalogue globalization has no functional dependency on Phases 1–2 and no
phase functionally depends on it to be *correct* — ADR-0004 already
establishes safe authorization behaviour (product/listing reads unrestricted
by project ownership) regardless of whether the underlying schema has been
globalized. It is not itself a numbered phase because it delivers no new
end-user capability; it is a data-model correction.

**Recommended before Phase 3** — cheaper to migrate now than after real
distinct users start accumulating their own item-scoped catalogue entries at
volume, and it removes the need for ADR-0004's "known gap" carve-out
entirely rather than living alongside it indefinitely.

**Hard blocking dependency before Phase 4 and Phase 5** — once the public
surface (Phase 4) and project cloning (Phase 5) are live, per-item catalogue
fragmentation stops being an internal wrinkle and becomes a visible,
compounding product-quality problem (duplicate products, fragmented price
history across many real users, clone-by-copy where clone-by-reference is
correct). **Phase 4 and Phase 5 should not ship to production before
Catalogue Globalization has landed.**

**Explicitly rejected:** a big-bang SaaS rewrite. No phase requires rewriting
the SQLite/WAL storage model, the `Source` connector contract, or the
scoring/catalogue/identity pipeline. All five phases are additive to the
existing domain model described in `docs/architecture-briefing.md`.

## Consequences

- Existing local, single-user, no-auth behaviour must keep working, unchanged
  by default, through Phases 1–2, and only changes in Phase 3 when ownership
  is intentionally introduced (with a migration, not a breaking change).
- Each phase's ADR and backlog epic records its own deferred scope — nothing
  here should be read as "and therefore Phase N+1 is committed" beyond the
  ordering above; each phase should still be independently justified when it
  starts.
- Subscriptions/billing are explicitly **not** implemented in any of these five
  phases. Where a natural hook exists (typically: a gated action in Phase 4),
  the hook is added as a no-op / always-allowed flag, not a payment integration.

## Deferred (across all phases)

- Subscriptions, billing, entitlements enforcement (hooks only — see ADR-0005).
- Real-time multi-editor collaboration on a shared project (Phase 5 delivers
  cloning, not co-editing).
- Public API.
- Mobile app / browser extension.
- Role-based access control beyond owner/recipient/anonymous.
