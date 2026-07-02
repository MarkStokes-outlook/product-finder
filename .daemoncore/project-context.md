# Project Context

## Project Name
daemon-core

## Description
<!-- Add a brief description of this repository -->

## Workspace Ownership & Authority
- Owner: <!-- Add owner or team name -->
- This workspace is subordinate to the DaemonCore kernel safety rules.
- Do not assume authority outside this repository without explicit human instruction.

## Tech Stack
- <!-- Add language / framework -->
- <!-- Add infrastructure / services -->

## Purpose of This Workspace
- Define how agents should treat this codebase.
- Capture architectural intent, boundaries, and domain concepts.
- Anchor planning and backlog work in a single, human-owned source of truth.

## Safety & Boundaries
- Do not modify build/release pipelines without explicit human approval.
- Avoid destructive operations (deletes, migrations, data transforms) unless explicitly requested.
- Treat secrets, credentials, and personal data as off-limits unless clearly provided for a task.
- When unsure, propose a plan in `.daemoncore/outbox/` instead of changing core project files.
