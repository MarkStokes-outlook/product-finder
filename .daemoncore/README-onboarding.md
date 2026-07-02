# Repo Onboarding

## Purpose
Quickstart guide for humans and agents using this repository with the DaemonCore kernel.

## Boot Order (Conceptual)
1. Global kernel boot: `~/Source/.daemoncore/kernel/boot.md`
2. Global runtime & modules: `~/Source/.daemoncore/runtime/**`
3. Project context: `.daemoncore/project-context.md`
4. Project constraints: `.daemoncore/project-AGENTS.md`
5. Project memory & conventions: `.daemoncore/project-memory.md`

## For Agents
When engaging with this repo:

1. Read `.daemoncore/project-context.md` to understand what this workspace is for.
2. Read `.daemoncore/project-AGENTS.md` to understand where you may and may not write.
3. Skim `.daemoncore/project-memory.md` for local conventions.
4. Prefer writing drafts, reports, and proposals into `.daemoncore/outbox/` unless told otherwise.

## For Humans
- Keep these `.daemoncore/*.md` files up to date; they are the contract between you and the kernel.
- Use `cognition/ideas` for pre-backlog exploration.
- Use `cognition/backlog` for canonical work items when you adopt the full backlog model.
