# ADR-0003: Authentik/OIDC as the authentication backend

**Status:** Proposed
**Date:** 2026-07-08
**Phase:** 2 of 5 (see ADR-0001)
**Related backlog:** EPIC-102 (FEATURE-1020..1023)

## Context

`docs/strategy/roadmap.md` ("Users and saved projects") already states intent:
"long term, authentication should be handled by Authentik/OIDC rather than a
bespoke password system. If implementation needs a stepping stone, a minimal
internal user model may be acceptable, but it should be shaped so it can be
replaced or backed by Authentik without rewriting project ownership,
permissions, or saved-state logic."

Today the web app (`src/product_finder/web/app.py`) has no authentication at
all — its own module docstring states "Local web UI. Flask, server-rendered,
localhost only, no auth by design." The Flask `secret_key` exists only to
support flash-message session cookies (`app.py`, line ~301).

## Decision

Integrate **Authentik** as an external OIDC provider using a standard
authorization-code flow (an OIDC client library such as `authlib`, added as a
new optional dependency). Add:

- `GET /auth/login` — redirects to Authentik's authorization endpoint.
- `GET /auth/callback` — exchanges the code, validates the ID token, establishes a session.
- `POST /auth/logout` — clears the session, optionally redirects to Authentik's end-session endpoint.
- A server-side session (still the existing Flask cookie session, now
  carrying an authenticated identity rather than only flash messages).

**This phase deliberately does not tie data ownership to the authenticated
identity** — per the user's explicit guidance for Phase 2. It introduces a
`users` table (`id`, `oidc_subject`, `email`, `display_name`, `created_at`)
purely as the identity record that Phase 3 (ADR-0004) will later attach
ownership to. After this phase, every request has an optional, nullable
`current_user` and nothing yet reads or restricts on it.

**Local/dev fallback:** when `auth.enabled=false` (the default for existing
installs, preserving current behaviour exactly), no login is required and no
Authentik configuration is needed — this satisfies "keep local/dev auth
simple where useful" and "existing listing/local behaviour must remain
compatible." `auth.enabled=false` is the only supported "no login" mode; there
is no bespoke password system as a permanent parallel path.

**Config additions** (`config.yaml` / environment): `auth.enabled`,
`auth.issuer`, `auth.client_id`, `auth.client_secret` (secret — environment
only, never committed, never DB-stored in plaintext), `auth.redirect_uri`.

## Consequences

- With `auth.enabled=false` (default), the app behaves exactly as it does
  today — this is the compatibility guarantee for existing local installs.
- With `auth.enabled=true`, users can log in/out and a session persists, but
  no page's visible content or permitted actions change yet — Phase 3 and
  Phase 4 are what make login consequential.
- The Flask `secret_key` now protects an authenticated session, not just
  flash messages — its handling (must be a real secret in production, not
  the current placeholder-friendly default) becomes a genuine security
  requirement from this phase onward, not a cosmetic one.

## Alternatives considered

- **Bespoke username/password with local hashing** — rejected as the primary
  path; explicitly ruled out by the roadmap and by the user's brief. Allowed
  only as a narrow, explicitly-flagged dev stepping stone if plumbing genuinely
  requires it, never as production auth.
- **Tying ownership to identity in this same phase** — rejected per explicit
  user guidance; keeps this phase's blast radius to "can I log in and out",
  which is independently testable and revertable without touching data access.

## Deferred

- MFA policy (owned by Authentik itself, not this app).
- SCIM/automated user provisioning.
- Role/permission scopes beyond "authenticated" vs "anonymous" (no RBAC yet).
- Any change to what a logged-in user can see or do (Phase 3/4).
