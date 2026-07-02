# Project Agents

These constraints apply to all agents operating within this repository under the DaemonCore kernel. This file defines project‑level operating rules, including MAX Bus v1.1 behaviour, message routing, and safety constraints.

## Scope
- Kernel‑level safety and CKC lineage always take precedence.
- These rules apply **only within this project** (project scope). Global behaviour is defined elsewhere.
- Agents must assume **project queue usage by default**:
  - All project work is routed via `.daemoncore/exchange/queues/` (project-local).

## MAX Bus v1.1 Messaging Rules

### Role-Based Routing
Agents MUST:
- Determine their active **role** from boot context (`developer`, `reviewer`, `orchestrator`, `architect`, or `system`).
- Read ONLY from: `.daemoncore/exchange/queues/<role>/pending/`
- Write ONLY to: `.daemoncore/exchange/queues/<target-role>/pending/`

Vendor-to-vendor or agent-specific queues are forbidden.

### Message Claiming
When processing a message, agents MUST:
1. Atomically move the file from `pending/` → `processing/` before reading.
2. Never read messages directly from `pending/`.
3. Move the message to:
   - `processed/` on success  
   - `failed/` on recoverable failure  
   - `dead-letter/` after max retries (handled by orchestrator/human)

### Scope Rules
- **Always use project queues** unless explicitly instructed to operate globally.
- Global queues are for **system-wide operations only** and must not be used for project tasks.

### Session Lock Compliance
If `.daemoncore/exchange/session-lock.md` exists:
- Agents MUST stop:
  - reading messages  
  - writing messages  
  - claiming messages  
- All queue activity halts until lock is removed.

## Write Permissions
Agents MAY write or modify:
- `.daemoncore/outbox/` — drafts, reports, generated artefacts.
- `.daemoncore/cognition/backlog/` — backlog data only.
- `.daemoncore/logs/` — session logs.
- `.daemoncore/cache/` — reproducible, transient cache data.

Agents MUST NOT modify without explicit human approval:
- `.git/` or version‑control internals.
- Tool/editor configs (`.vscode/`, `.idea/`, etc.).
- CI/CD config, deployment manifests, or infrastructure code.
- Secrets, credentials, environment files, or identity material.

## Behaviour Rules
- Prefer additive changes (new sections, new files) over destructive rewrites.
- For complex or multi-step changes:
  - Propose a plan.
  - Generate drafts in `.daemoncore/outbox/`.
  - Apply only after human approval.
- If a task is ambiguous, unsafe, or outside role capability—stop and request clarification.

## Escalation Protocol
If an action conflicts with:
- kernel safety rules,  
- CKC lineage,  
- project constraints, or  
- MAX Bus v1.1 routing/lifecycle rules,

the agent MUST:
1. Refuse the unsafe action.
2. Explain why the action violates rules.
3. Offer a safe alternative or plan for review.
