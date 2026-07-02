# Ideas Workspace (Non-Canonical Staging)

This directory mirrors the backlog detail folders but is **not** part of the
canonical planning state. Product leads can stage future Epics, Features, User
Stories, and Tasks here before they are validated and promoted into
`.daemoncore/cognition/backlog/`.

Key rules:
- Files must follow the same schemas as their backlog counterparts.
- Epics may use `status: "idea"`; stories may start at `seed`, but the backlog
  entry is created only after promotion.
- No planner, scheduler, or agent reads executable work from this workspace.
- When promoting an idea move the file into the backlog folder and update
  `backlog.json` accordingly. The copy left here becomes archival only.

## Directory layout

```
.daemoncore/cognition/ideas/
  README.md
  _templates/
  epics/
  features/
  user-stories/
  tasks/
```

Use the JSON templates in `_templates/` when drafting new ideas. Copy the
appropriate file, assign a temporary ID (e.g. `IDEA-001`,
`FEATURE-9xx-US-yy`), and store it in the matching entity folder.
