# Claude Code — product-finder

## Purpose

This file instructs Claude Code on the proper boot sequence and working rules
when operating in this project.

## Boot Order (Mandatory)

1. **Global Kernel Boot**
   Load: `/Users/markstokes/Source/.daemoncore/kernel/kernel.md` (operational kernel contract, first)
   Load: `/Users/markstokes/Source/.daemoncore/kernel/boot.md` (global boot protocol, after kernel.md)
   Initialises tone, memory, global context, and core kernel modules.

2. **Project Boot**
   Load: `.daemoncore/boot.md`
   Project-level boot sequence and context.

3. **Vendor Init**
   Load: `.daemoncore/vendors/claude/init.md`
   Project-specific Claude guidance.

## Mission

[Describe the primary mission and purpose of this project]

## Project Structure

[Document key directories and files]

## Operating Mode

- **Context**: product-finder
- **Scope**: [What this project does]
- **Tech Stack**: [Languages, frameworks, tools]
- **Current Phase**: [Development stage or current focus]

## Working Rules

1. **Explicit changes** — Always show paths and diffs
2. **Respect boundaries** — Follow domain constraints in `.daemoncore/domain.md`
3. **Ask when unclear** — Clarify ambiguous requirements
4. **Documentation** — Keep docs in sync with changes

## Boot Confirmation

After loading all context, confirm:
- Repo: `product-finder`
- Branch: [current branch]
- Context loaded: kernel, project boot, vendor init
- Ready to proceed with task

## Notes

- Follow project conventions documented in `.daemoncore/project-context.md`
- Update `.daemoncore/project-memory.md` after significant actions
- Use `.daemoncore/cognition/` for work planning
