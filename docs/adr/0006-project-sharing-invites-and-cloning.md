# ADR-0006: Project sharing, invites, and cloning semantics

**Status:** Proposed
**Date:** 2026-07-08
**Phase:** 5 of 5 (see ADR-0001)
**Related backlog:** EPIC-105 (FEATURE-1050..1053)
**Depends on:** ADR-0007 / EPIC-100 (Catalogue globalization) — hard blocking dependency per ADR-0001; clone-by-reference (see Decision, Clone section) requires it to have landed.

## Context

Phase 3 (ADR-0004) established single-owner projects (`projects.owner_user_id`).
There is currently no way for an owner to give another person access to a
project. The user's brief is explicit that real-time co-editing is out of
scope for this phase — the two supported primitives are **sharing by
invite** and **cloning**, and they must be clearly distinguished.

## Decision

Two distinct, separately-testable mechanisms:

### 1. Share (invite)

An owner shares a project with an email address. A `project_invites` row is
created: `id`, `project_id`, `inviter_user_id`, `invitee_email`,
`invitee_user_id` (nullable until resolved), `status`
(`pending`/`accepted`/`declined`/`revoked`), `created_at`, `resolved_at`.

- If `invitee_email` matches an existing user, the invite is immediately
  associated (`invitee_user_id` set) and that user sees a pending invite.
- If the email is **not** registered, the invite is still created
  (`invitee_user_id` NULL, `status=pending`) and is **not lost** — it
  resolves automatically the first time a user completes signup/login
  (Phase 2's Authentik flow) with that email address.
- The owner can revoke a pending invite at any time before acceptance.
- No collaborative editing role is introduced — an accepted invite's only
  defined outcome in this phase is enabling a **clone** (below), not shared
  write access to the original project.

### 2. Clone ("send project to")

A distinct action: the recipient of an accepted invite receives an
independent **copy**, not shared access to the original.

- A new `projects` row is created, owned by the recipient (`owner_user_id =
  recipient`). If Catalogue Globalization (ADR-0007, EPIC-100) has landed by
  the time this phase ships — a hard blocking dependency, see ADR-0001 —
  cloning **references** the recipient's new `item_products` rows at the
  same global `products` entries, copying nothing catalogue-shaped at all.
  If it has somehow not landed (not expected, given ADR-0001 blocks this
  phase on it), the fallback is to deep-copy `items` and their item-scoped
  `products` rows, which reflects today's schema, not a statement that
  products are user-owned. Items' own terms/prices/notes are copied as part
  of the item either way.
- **Listings, listing_matches, price observations/history (including
  `product_price_observations`/`product_new_price_history`), and auction
  snapshots are NOT copied.** The clone starts with no listings and no
  accumulated price evidence; its own watch cycle populates them
  independently. This avoids attributing one owner's observed-market
  evidence to another owner's project, and keeps the clone a genuine
  independent project rather than a shared view.
- **Forward-looking note:** if/when catalogue globalization (ADR-0004,
  "Known schema gap") lands and `products` becomes a true shared table, a
  clone should *reference* the same global product rather than duplicate the
  row — this ADR's "copy products" behaviour is scoped to today's schema and
  should be revisited alongside that follow-up, not treated as a precedent
  that products are per-project data.
- The clone and the original are never linked for write purposes after
  creation — editing one never mutates the other.

### Semantics (explicit, per the user's request)

- **Owner** — `projects.owner_user_id`; the only account that can edit,
  share, revoke invites for, or delete a project.
- **Recipient** — the user who accepts an invite and ends up owning the
  resulting cloned project. A recipient is never a co-owner of the original.
- **Invite** — a pending grant tied to an email address, resolvable to a
  user account at signup/login time if not already registered, revocable by
  the owner any time before acceptance.
- **Clone** — a deep, point-in-time copy of a project's configuration
  (items/products/terms/notes), explicitly excluding accumulated listing
  evidence.
- **Visibility** — an invited-but-not-yet-accepted recipient sees only
  enough to decide whether to accept (project name, a short summary) —
  never the full project contents before acceptance.

## Consequences

- No shared-write model means no conflict-resolution or concurrent-edit
  complexity — this is a deliberate scope reduction, not an oversight; it
  matches the user's instruction to keep collaboration/editing "separate and
  deferred unless explicitly required."
- A clone diverges from its source immediately and permanently; there is no
  "pull updates from original" mechanism in this phase.
- Invite state for unregistered emails must be considered at every future
  point that touches user signup (Phase 2's callback) — signup must check
  for and resolve pending invites by email, or invites silently rot.

## Alternatives considered

- **Shared ownership (multiple `owner_user_id` values per project, e.g. a
  join table)** — rejected for this phase: reintroduces concurrent-edit
  semantics the user explicitly deferred; may be revisited later as a
  genuinely new phase, not folded into this one.
- **Cloning without an invite step (direct "copy to any email")** — rejected:
  loses the authorization boundary and the "recipient chose to accept" record
  that the invite lifecycle provides, and complicates the unregistered-email
  case (would require creating a shadow account rather than resolving to a
  real one at signup).

## Deferred

- Real-time co-editing / multi-owner projects.
- Granular per-item sharing (sharing a single item rather than a whole project).
- Org/team-level sharing.
- Re-sync or "pull updates" from the original project into a clone after creation.

## Required tests (explicit, not optional for this phase)

- Authorization: a non-owner cannot share, revoke, or clone-approve on
  someone else's project; a recipient cannot see project contents before
  accepting.
- Invite lifecycle: pending → accepted/declined/revoked; re-invite after a
  decline; an invite created for an unregistered email resolves correctly
  once that email completes signup.
- Clone integrity: the clone is a fully independent deep copy — mutating
  either project never affects the other; listings/matches/price history are
  confirmed absent from a freshly cloned project.
- End-to-end non-registered-invite flow: invite created for an unknown email
  → that email signs up via Authentik (Phase 2) → invite auto-resolves and
  becomes actionable by the new user.
