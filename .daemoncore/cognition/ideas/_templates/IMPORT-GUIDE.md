# Ideas Lab Import Guide

## Overview

This guide explains how to import work items into the Ideas Lab using JSON files. Ideas Lab is a staging area for work items before they are promoted to the canonical backlog.

**⚠️ VALIDATOR IS SOURCE OF TRUTH**

This documentation describes the import validator's rules. If documentation and validator behaviour differ, **the validator wins**. The validator code is the canonical source:
- **Validator:** `src/runtime/web/api_routes/project.py:1632` (`_validate_import_item` function)
- **Enums:** `src/runtime/cognition/models.py:23-150` (Status, Priority, Size, WorkType, Severity, Environment, Source)

When adding or changing allowed values, update the validator code first, then this guide.

## Quick Start

1. Copy `import-example.json` as your starting point
2. Modify the values to match your work items
3. Import using the web UI or CLI command
4. Review and edit items in Ideas Lab
5. Promote to backlog when ready

## File Format

Import files must be valid JSON arrays containing work item objects:

```json
[
  { "id": "IDEA-EPIC-001", "type": "epic", ... },
  { "id": "IDEA-FEAT-001", "type": "feature", ... }
]
```

## Case Sensitivity and Formatting Rules

**CRITICAL:** The validator enforces strict case and formatting rules:

| Field | Format | Example Valid | Example Invalid |
|-------|--------|---------------|-----------------|
| `type` | lowercase | `epic`, `feature`, `story`, `task` | `Epic`, `TASK`, `user-story` |
| `status` | lowercase, underscores | `idea`, `in_progress`, `todo` | `Idea`, `IN_PROGRESS`, `in-progress` |
| `priority` | uppercase | `P1`, `P2`, `P3`, `P4` | `p1`, `P0` |
| `size` | uppercase | `XS`, `S`, `M`, `L`, `XL` | `xs`, `s`, `m` |
| `work_type` | lowercase | `feature`, `bug`, `chore`, `spike`, `incident` | `Feature`, `BUG`, `refactor` |
| `severity` | lowercase | `blocker`, `critical`, `major`, `minor`, `trivial` | `Blocker`, `HIGH` |
| `environment` | lowercase | `dev`, `test`, `staging`, `prod`, `other` | `Dev`, `PROD` |
| `source` | lowercase, underscores | `ci`, `monitoring`, `user_report`, `internal_test`, `code_review` | `CI`, `user-report` |

**Formatting rules:**
- Multi-word values use **underscores** (`in_progress`, `user_report`), never hyphens (`in-progress`)
- Priority and Size are **uppercase** (`P1`, `XL`), all other enums are **lowercase**
- Type `story` (not `user-story` or `user_story`)

## ID Conventions

All IDs in Ideas Lab should use the `IDEA-` prefix:

| Type | ID Format | Example |
|------|-----------|---------|
| Epic | `IDEA-EPIC-NNN` | `IDEA-EPIC-001` |
| Feature | `IDEA-FEAT-NNN` | `IDEA-FEAT-001` |
| User Story | `IDEA-US-NNN` | `IDEA-US-001` |
| Task | `IDEA-TASK-NNN` | `IDEA-TASK-001` |

**Note:** When promoted to backlog, items will be assigned canonical IDs (e.g., `EPIC-0903`, `FEATURE-3024`, `US-0042`, `TASK-0123`).

## Work Item Types

### Epic

**Required Fields:**
- `id` - Unique identifier (e.g., `IDEA-EPIC-001`)
- `type` - Must be `"epic"`
- `title` - Short, descriptive title
- `status` - Use `"idea"` for Ideas Lab imports

**Optional Fields:**
- `description` - High-level context and strategic value
- `rationale` - Why this epic is needed (business case)
- `priority` - Priority level (`P0`, `P1`, `P2`, `P3`, `P4`)
- `tags` - Array of tags for categorization
- `feature_ids` - Array of feature IDs (populated on promotion)
- `success_criteria` - Array of measurable success criteria
- `notes` - Free-form planning notes
- `phase` - Numeric phase identifier (for wave planning)

**Example:**
```json
{
  "id": "IDEA-EPIC-100",
  "type": "epic",
  "title": "Payment Processing System",
  "description": "Build comprehensive payment infrastructure supporting multiple payment methods",
  "rationale": "Enable revenue generation through multiple payment channels while maintaining PCI compliance",
  "status": "idea",
  "priority": "P1",
  "tags": ["payments", "infrastructure"],
  "feature_ids": [],
  "success_criteria": [
    "Process payments with 99.9% uptime",
    "Support Visa, Mastercard, Amex, and PayPal",
    "Achieve PCI DSS Level 1 compliance"
  ],
  "notes": "Consider PCI compliance requirements and payment gateway redundancy",
  "phase": 1
}
```

### Feature

**Required Fields:**
- `id` - Unique identifier (e.g., `IDEA-FEAT-001`)
- `type` - Must be `"feature"`
- `epic_id` - Parent epic ID (e.g., `IDEA-EPIC-001`)
- `title` - Feature name
- `status` - Use `"idea"` for Ideas Lab imports (can also use `"backlog"`)

**Optional Fields:**
- `description` - Feature context and scope
- `version` - Target version (e.g., "1.0.0", "2.1.0-alpha")
- `priority` - Priority level (`P0`, `P1`, `P2`, `P3`, `P4`)
- `domain` - Domain classification (see Valid Domains below)
- `area` - Area within domain (see Valid Areas below)
- `tags` - Array of tags
- `user_story_ids` - Array of user story IDs (populated on promotion)
- `acceptance_criteria` - Array of measurable conditions
- `prompt_path` - Path to feature-level prompt file (optional)
- `notes` - Planning commentary
- `phase` - Numeric phase identifier (for wave planning)

**Example:**
```json
{
  "id": "IDEA-FEAT-200",
  "type": "feature",
  "epic_id": "IDEA-EPIC-100",
  "title": "Credit Card Processing",
  "description": "Accept and process credit card payments via Stripe",
  "version": "1.0.0",
  "status": "idea",
  "priority": "P1",
  "domain": "web-api",
  "area": "auth",
  "tags": ["stripe", "payments"],
  "user_story_ids": [],
  "acceptance_criteria": [
    "Support Visa, Mastercard, and Amex",
    "Handle payment failures gracefully",
    "Store payment methods securely"
  ],
  "prompt_path": null,
  "notes": "Ensure PCI DSS compliance throughout implementation",
  "phase": 1
}
```

### User Story

**Required Fields:**
- `id` - Unique identifier (e.g., `IDEA-US-001`)
- `type` - Must be `"story"` (NOT "user-story")
- `feature_id` - Parent feature ID (e.g., `IDEA-FEAT-001`)
- `title` - User-facing statement
- `status` - Use `"idea"` for Ideas Lab imports (NOT "seed")

**Optional Fields:**
- `story` - User story format: "As a... I want... so that..."
- `description` - Expanded context
- `priority` - Priority level (`P0`, `P1`, `P2`, `P3`, `P4`)
- `size` - Story size (`XS`, `S`, `M`, `L`, `XL`)
- `impact_area` - Area of impact (e.g., "operator-experience", "agent-capability")
- `domain` - Domain classification
- `area` - Area within domain
- `tags` - Array of tags
- `task_ids` - Array of task IDs (populated on promotion)
- `acceptance_criteria` - Array of testable conditions
- `implementation_hints` - Array of technical guidance
- `affected_files` - Array of file paths expected to be modified
- `notes` - Clarifications or considerations
- `phase` - Numeric phase identifier (for wave planning)

**Example:**
```json
{
  "id": "IDEA-US-300",
  "type": "story",
  "feature_id": "IDEA-FEAT-200",
  "title": "Process one-time credit card payment",
  "story": "As a customer, I want to pay with my credit card so that I can complete my purchase quickly.",
  "description": "Single payment flow for one-time purchases without saving card details.",
  "status": "idea",
  "priority": "P1",
  "size": "M",
  "impact_area": "customer-experience",
  "domain": "web-api",
  "area": "auth",
  "tags": ["stripe", "checkout"],
  "task_ids": [],
  "acceptance_criteria": [
    "Display secure payment form with card inputs",
    "Validate card details before submission",
    "Show clear error messages for failed payments",
    "Confirm successful payment with order number"
  ],
  "implementation_hints": [
    "Use Stripe Elements for PCI compliance",
    "Implement proper error handling for network failures"
  ],
  "affected_files": [
    "src/runtime/web/api_routes/payments.py",
    "src/runtime/web/ui/components/PaymentForm.tsx"
  ],
  "notes": "Consider storing payment intent ID for reconciliation",
  "phase": 1
}
```

### Task

**Required Fields:**
- `id` - Unique identifier (e.g., `IDEA-TASK-001`)
- `type` - Must be `"task"`
- `user_story_id` - Parent story ID (e.g., `IDEA-US-001`)
- `title` - Discrete, actionable work
- `status` - Use `"idea"` for Ideas Lab imports (can also use `"todo"`)
- `prompt` - Markdown-formatted execution instructions for agents (creates `.prompt` file)

**Optional Fields:**
- `description` - Implementation details
- `priority` - Priority level (`P0`, `P1`, `P2`, `P3`, `P4`)
- `size` - Task size (`XS`, `S`, `M`, `L`, `XL`) - **critical for wave capacity planning**
- `work_type` - Type of work (`feature`, `bug`, `refactor`, `tech-debt`, `docs`, `test`, `spike`)
- `severity` - Bug severity (only for bugs/incidents: `critical`, `high`, `medium`, `low`)
- `domain` - Domain classification
- `area` - Area within domain
- `tags` - Array of tags
- `depends_on` - Array of task IDs this depends on - **critical for wave ordering**
- `links` - Array of cross-reference links (see Link structure below)
- `detected_in_version` - Version where bug was found (for bugs/incidents)
- `fixed_in_version` - Version where bug was fixed (for bugs/incidents)
- `environment` - Where issue occurred: `dev`, `test`, `staging`, `prod`, `other` (for bugs/incidents)
- `source` - Origin: `ci`, `monitoring`, `user_report`, `internal_test`, `code_review`, `other`
- `target_files` - Array of file paths to modify
- `target_directories` - Array of directory paths
- `repo` - Repository name (if different from default)
- `branch` - Target branch name
- `execution_hints` - Execution hints object (see ExecutionHints structure below)
- `acceptance_criteria` - Array of completion criteria
- `notes` - Implementation notes
- `phase` - Numeric phase identifier (for wave planning) - **used to group tasks into waves**

**Example (Feature Work):**
```json
{
  "id": "IDEA-TASK-400",
  "type": "task",
  "user_story_id": "IDEA-US-300",
  "title": "Create Stripe payment intent endpoint",
  "description": "Implement POST /api/payments/intent endpoint that creates a Stripe PaymentIntent and returns client_secret.",
  "status": "todo",
  "priority": "P1",
  "size": "S",
  "work_type": "feature",
  "severity": null,
  "domain": "web-api",
  "area": "auth",
  "tags": ["stripe", "backend"],
  "depends_on": [],
  "links": [
    {
      "type": "related_task",
      "target": "IDEA-TASK-401"
    }
  ],
  "detected_in_version": null,
  "fixed_in_version": null,
  "environment": null,
  "source": null,
  "target_files": [
    "src/runtime/web/api_routes/payments.py"
  ],
  "target_directories": [
    "src/runtime/web/api_routes/"
  ],
  "repo": null,
  "branch": null,
  "execution_hints": {
    "type": "local",
    "agent_preference": null,
    "requires_approval": false,
    "idempotent": true
  },
  "acceptance_criteria": [
    "Endpoint validates amount and currency",
    "Returns valid Stripe client_secret",
    "Handles Stripe API errors gracefully",
    "Unit tests cover success and error cases"
  ],
  "notes": "Use stripe.PaymentIntent.create() with idempotency_key for safety",
  "phase": 1,
  "prompt": "# Create Stripe Payment Intent Endpoint\n\n## Goal\nImplement POST /api/payments/intent endpoint that creates a Stripe PaymentIntent.\n\n## Requirements\n- Validate amount and currency parameters\n- Create PaymentIntent via Stripe API\n- Return client_secret to frontend\n- Handle Stripe API errors gracefully\n\n## Technical Approach\n1. Add route handler in payments.py\n2. Use stripe.PaymentIntent.create() with idempotency_key\n3. Return structured JSON response\n\n## Acceptance Criteria\n- [ ] Endpoint validates amount and currency\n- [ ] Returns valid Stripe client_secret\n- [ ] Handles Stripe API errors gracefully\n- [ ] Unit tests cover success and error cases"
}
```

**Example (Bug Work):**
```json
{
  "id": "IDEA-TASK-401",
  "type": "task",
  "user_story_id": "IDEA-US-300",
  "title": "Fix payment timeout handling",
  "description": "Payment intent creation hangs when Stripe API is slow, causing 504 gateway timeouts.",
  "status": "todo",
  "priority": "P0",
  "size": "XS",
  "work_type": "bug",
  "severity": "high",
  "domain": "web-api",
  "area": "auth",
  "tags": ["stripe", "bug", "timeout"],
  "depends_on": [],
  "links": [
    {
      "type": "external_issue",
      "target": "github:daemon-core#1234"
    }
  ],
  "detected_in_version": "1.0.0",
  "fixed_in_version": null,
  "environment": "prod",
  "source": "monitoring",
  "target_files": [
    "src/runtime/web/api_routes/payments.py"
  ],
  "target_directories": [],
  "repo": null,
  "branch": null,
  "execution_hints": {
    "type": "local",
    "agent_preference": null,
    "requires_approval": true,
    "idempotent": true
  },
  "acceptance_criteria": [
    "Add 10-second timeout to Stripe API calls",
    "Return 503 with retry-after header on timeout",
    "Log timeout events for monitoring"
  ],
  "notes": "Production issue affecting checkout conversion rate",
  "phase": 1,
  "prompt": "# Fix Payment Timeout Handling\n\n## Goal\nAdd timeout handling to Stripe PaymentIntent API calls.\n\n## Context\nPayment intent creation hangs when Stripe API is slow (>30s), causing 504 gateway timeouts and failed checkouts.\n\n## Requirements\n- Add 10-second timeout to stripe.PaymentIntent.create()\n- Return 503 with Retry-After header on timeout\n- Log timeout events with request context\n\n## Technical Approach\n1. Wrap Stripe API call in timeout context\n2. Catch timeout exception\n3. Return structured 503 response\n4. Add metrics/logging\n\n## Acceptance Criteria\n- [ ] Stripe API calls timeout after 10 seconds\n- [ ] Returns 503 with Retry-After: 60 header\n- [ ] Timeout events logged with request ID"
}
```

## Complex Field Types

### Link Object

Cross-reference links between entities or to external resources:

```json
{
  "type": "link_type",
  "target": "target_reference"
}
```

**Fields:**
- `type` - Link type: `depends_on`, `related_task`, `external_issue`, `adr`, etc.
- `target` - Entity ID or external reference (e.g., `"github:daemon-core#1234"`)

**Example:**
```json
"links": [
  {
    "type": "related_task",
    "target": "IDEA-TASK-402"
  },
  {
    "type": "external_issue",
    "target": "github:daemon-core#1234"
  }
]
```

### ExecutionHints Object

Agent execution hints for task automation:

```json
{
  "type": "execution_type",
  "agent_preference": "agent_name",
  "requires_approval": false,
  "idempotent": true
}
```

**Fields:**
- `type` - Execution type: `local` (default), `remote`, `adapter`
- `agent_preference` - Preferred agent name (optional)
- `requires_approval` - Whether human approval required before execution
- `idempotent` - Whether task can be safely re-executed

**Example:**
```json
"execution_hints": {
  "type": "local",
  "agent_preference": null,
  "requires_approval": false,
  "idempotent": true
}
```

## Valid Domains and Areas

When items are promoted from Ideas Lab to Backlog, they must have valid `domain` and `area` combinations. During import, you can:
- Set them to valid values (recommended)
- Leave them as `null` (you'll need to set them before promotion)

### Valid Domains

- `dev-factory` - Development tooling and infrastructure
- `kernel` - Core kernel functionality
- `web-api` - Backend API services
- `web-ui` - Frontend UI components
- `cognition` - Backlog and planning system
- `infra` - Infrastructure and deployment
- `ops` - Operations and monitoring

### Valid Areas by Domain

**dev-factory:**
- `backlog-loader`, `scheduler`, `agents`, `github-integration`

**kernel:**
- `cli`, `config`, `execution`, `safety`

**web-api:**
- `work-endpoints`, `auth`, `health`, `projects`

**web-ui:**
- `operator-console`, `components`, `navigation`

**cognition:**
- `backlog-model`, `migration`, `planning`, `ideas-workspace`

**infra:**
- `docker`, `networking`, `observability`

**ops:**
- `monitoring`, `alerts`, `runtime-analysis`

**Note:** `domain` can be `null`, but if you set an `area`, the `domain` must be valid and support that area.

## Field Validation Rules

### Type Values (Required)

All items must have a `type` field with one of these exact values:
- `epic`
- `feature`
- `story` (NOT "user-story")
- `task`

### Status Values

**Import validator accepts these status values for ALL types (lowercase, underscore-separated):**
- `idea` - Item is in ideation phase (**recommended default for Ideas Lab imports**)
- `backlog` - Item is in backlog
- `todo` - Item is ready to work on
- `in_progress` - Work is in progress (**use underscore, NOT hyphen**)
- `blocked` - Work is blocked
- `done` - Work is completed

**❌ BACKLOG-ONLY STATUSES (NOT VALID FOR IMPORT):**

These statuses exist in the backlog system but are **NOT accepted** by the import validator:
- `seed` - Legacy user story status (backlog-only)
- `ready` - Legacy user story status (backlog-only)
- `planned` - Epic/Feature lifecycle status (backlog-only)
- `ready_for_review` - Task review lifecycle status (backlog-only)
- `changes_required` - Task review lifecycle status (backlog-only)
- `cancelled` - Terminal status (backlog-only)

**Use `idea` or `todo` instead.** These backlog statuses are managed by the system during the work item lifecycle and cannot be set during import.

**❌ COMMON FORMATTING ERRORS:**
- `in-progress` → **WRONG** (hyphen) - Use `in_progress` (underscore)
- `IN_PROGRESS` → **WRONG** (uppercase) - Use `in_progress` (lowercase)
- `Idea` → **WRONG** (capitalized) - Use `idea` (lowercase)

### Priority Values

**Valid values (case-sensitive, UPPERCASE required):**
- `P1` (High priority)
- `P2` (Normal priority - default)
- `P3` (Low priority)
- `P4` (Future / Nice to have)

**❌ NOT ACCEPTED:**
- `P0` - Exists in models but NOT accepted by import validator
- `p1`, `p2`, `p3`, `p4` - Lowercase not accepted (must be uppercase)

**Validator rule:** Priority is converted to uppercase before validation, so `P1` is required.

### Size Values

**Valid values (case-sensitive, uppercase):**
- `XS` (< 1 hour)
- `S` (1-4 hours)
- `M` (1-2 days - default)
- `L` (3-5 days)
- `XL` (> 1 week)

### Work Type Values (Tasks Only)

**Valid values (lowercase):**
- `feature` - New functionality (default)
- `bug` - Bug fix
- `chore` - Maintenance work
- `spike` - Research/investigation
- `incident` - Production incident

**Note:** `refactor`, `tech-debt`, `docs`, `test` are NOT accepted. Use `chore` for maintenance work and `feature` for new functionality.

### Severity Values (Bugs/Incidents Only)

**Valid values (lowercase):**
- `blocker` - Blocks all work
- `critical` - Critical severity
- `major` - Major impact
- `minor` - Minor impact
- `trivial` - Trivial issue

**Note:** Severity can ONLY be set when `work_type` is `bug` or `incident`.

## Import Workflow

### 1. Prepare Your Import File

Create a JSON file with your work items:

```bash
# Start from the template
cp .daemoncore/cognition/ideas/.templates/import-example.json my-import.json

# Edit with your data
# Ensure valid JSON syntax
```

### 2. Validate IDs

- All IDs must be unique within the import file
- Use `IDEA-` prefix for all IDs
- Ensure parent IDs reference items in the same import (or existing items)

### 3. Import via Web UI

1. Navigate to Ideas Lab page
2. Click "Import" button
3. Select your JSON file
4. Review validation results
5. Confirm import

### 4. Review Imported Items

After import:
- Check Ideas Lab page for all imported items
- Verify parent-child relationships are correct
- Edit any items as needed
- Set domain/area if not already set

### 5. Promote to Backlog

When ready:
1. Select items to promote (or promote entire epic)
2. Click "Promote All"
3. Choose whether to delete ideas after promotion (recommended)
4. Items appear in Backlog page with new canonical IDs

## Best Practices

### ID Management

✅ **Do:**
- Use sequential numbering (IDEA-EPIC-001, IDEA-EPIC-002, etc.)
- Keep numbering consistent across imports
- Document ID ranges for different projects/epics

❌ **Don't:**
- Reuse IDs from previous imports
- Mix numbering schemes
- Use canonical backlog IDs (EPIC-XXX) in Ideas Lab

### Hierarchical Structure

✅ **Do:**
- Define epics before features
- Define features before stories
- Define stories before tasks
- Set all parent IDs correctly

❌ **Don't:**
- Create orphaned items (missing parents)
- Reference IDs that don't exist
- Create circular dependencies

### Domain/Area Assignment

✅ **Do:**
- Set domain/area during import if known
- Use consistent domain/area for related items
- Check valid combinations before promotion

❌ **Don't:**
- Use invalid domain/area combinations
- Leave critical items with null domain/area
- Mix domains within a single feature

### Content Quality

✅ **Do:**
- Write clear, actionable titles
- Provide sufficient context in descriptions
- Include measurable acceptance criteria
- Add relevant tags for searchability

❌ **Don't:**
- Use vague or overly long titles
- Skip descriptions on complex items
- Forget acceptance criteria
- Over-tag or use inconsistent tags

## Troubleshooting

### Common Issues

**"Invalid parent ID"**
- Ensure parent item exists in import file or in Ideas Lab
- Check ID spelling and format

**"Domain/area validation failed"**
- Check domain is in valid list
- Verify area is valid for that domain
- Can be fixed in UI before promotion

**"Duplicate ID"**
- Check for duplicate IDs in import file
- Verify ID doesn't already exist in Ideas Lab

**"Invalid status"**
- Use valid status values for item type
- Check status spelling

**"JSON syntax error"**
- Validate JSON format (use online validator)
- Check for missing commas, quotes, or brackets
- Ensure proper array structure

## Common Validation Errors

This section shows real validation errors from the import validator with examples of incorrect and correct values.

### Invalid Type

❌ **Incorrect:**
```json
{
  "id": "IDEA-US-001",
  "type": "user-story",
  "title": "Example"
}
```
**Error:** `Invalid type 'user-story'`
**Detail:** Expected: epic, feature, story, task

✅ **Correct:**
```json
{
  "id": "IDEA-US-001",
  "type": "story",
  "title": "Example"
}
```

### Invalid Status

❌ **Incorrect:**
```json
{
  "id": "IDEA-US-001",
  "type": "story",
  "title": "Example",
  "status": "seed"
}
```
**Error:** `Invalid value 'seed'`
**Detail:** Expected: backlog, todo, in_progress, done, blocked, idea

✅ **Correct:**
```json
{
  "id": "IDEA-US-001",
  "type": "story",
  "title": "Example",
  "status": "idea"
}
```

### Invalid Status (Hyphen vs Underscore)

❌ **Incorrect:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "status": "in-progress"
}
```
**Error:** `Invalid value 'in-progress'`
**Detail:** Use underscore, not hyphen: `in_progress`

✅ **Correct:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "status": "in_progress"
}
```

### Invalid Priority

❌ **Incorrect:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "priority": "P0"
}
```
**Error:** `Invalid priority 'P0'`
**Detail:** Expected: P1, P2, P3, P4 (P0 not accepted)

✅ **Correct:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "priority": "P1"
}
```

### Invalid Priority (Case Sensitivity)

❌ **Incorrect:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "priority": "p2"
}
```
**Error:** `Invalid priority 'p2'`
**Detail:** Priority must be uppercase: P1, P2, P3, P4

✅ **Correct:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "priority": "P2"
}
```

### Invalid Size (Case Sensitivity)

❌ **Incorrect:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "size": "m"
}
```
**Error:** `Invalid size 'm'`
**Detail:** Size must be uppercase: XS, S, M, L, XL

✅ **Correct:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "size": "M"
}
```

### Invalid Work Type

❌ **Incorrect:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "work_type": "refactor"
}
```
**Error:** Work type 'refactor' not in validator's accepted list
**Detail:** Valid work types: feature, bug, chore, spike, incident

✅ **Correct:**
```json
{
  "id": "IDEA-TASK-001",
  "type": "task",
  "title": "Example",
  "work_type": "chore"
}
```

### Missing Required Parent

❌ **Incorrect:**
```json
{
  "id": "IDEA-FEAT-001",
  "type": "feature",
  "title": "Example Feature"
}
```
**Error:** `Required field missing: epic_id`
**Detail:** Features must specify a parent epic

✅ **Correct:**
```json
{
  "id": "IDEA-FEAT-001",
  "type": "feature",
  "epic_id": "IDEA-EPIC-001",
  "title": "Example Feature"
}
```

## Example: Complete Epic Import

Here's a complete example importing an epic with all child items:

```json
[
  {
    "id": "IDEA-EPIC-500",
    "type": "epic",
    "title": "Multi-tenant Support",
    "description": "Add multi-tenant isolation and management capabilities",
    "status": "idea",
    "tags": ["multi-tenant", "architecture"],
    "notes": "Coordinate with security team for compliance review",
    "phase": 2
  },
  {
    "id": "IDEA-FEAT-501",
    "type": "feature",
    "epic_id": "IDEA-EPIC-500",
    "title": "Tenant Database Isolation",
    "description": "Implement schema-based tenant isolation",
    "status": "idea",
    "domain": "infra",
    "area": "docker",
    "tags": ["database", "isolation"],
    "acceptance_criteria": [
      "Each tenant has isolated database schema",
      "Cross-tenant queries are prevented",
      "Migration tools support multi-tenant structure"
    ],
    "notes": "Evaluate schema-per-tenant vs database-per-tenant approaches",
    "phase": 2
  },
  {
    "id": "IDEA-US-502",
    "type": "story",
    "feature_id": "IDEA-FEAT-501",
    "title": "Create isolated tenant database",
    "story": "As a platform admin, I want to provision isolated databases for tenants so that data is completely separated.",
    "description": "Automated provisioning of tenant-specific database schemas during tenant creation workflow",
    "status": "idea",
    "priority": "P1",
    "size": "L",
    "impact_area": "platform-reliability",
    "domain": "infra",
    "area": "docker",
    "tags": ["provisioning", "tenant-management"],
    "acceptance_criteria": [
      "Schema created automatically on tenant signup",
      "Schema includes all required tables and indexes",
      "Rollback mechanism available for failed provisioning"
    ],
    "implementation_hints": [
      "Use Alembic for schema migrations",
      "Consider connection pooling per tenant"
    ],
    "notes": "Must handle schema creation failures gracefully",
    "phase": 2
  },
  {
    "id": "IDEA-TASK-503",
    "type": "task",
    "user_story_id": "IDEA-US-502",
    "title": "Create tenant schema creation script",
    "description": "Write migration script that creates isolated schema per tenant",
    "status": "idea",
    "priority": "P1",
    "size": "M",
    "work_type": "feature",
    "severity": null,
    "domain": "infra",
    "area": "docker",
    "tags": ["migration", "database"],
    "depends_on": [],
    "target_files": ["src/infra/tenants/schema.py"],
    "target_directories": ["src/infra/tenants/"],
    "acceptance_criteria": [
      "Script creates schema with all tables",
      "Script is idempotent",
      "Unit tests verify schema structure"
    ],
    "execution_hints": "Use SQLAlchemy metadata for schema definition",
    "notes": "Ensure script can be run in production safely",
    "phase": 2,
    "prompt": "# Create Tenant Schema Script\n\n## Goal\nImplement migration script that creates an isolated database schema for each tenant.\n\n## Requirements\n- Create schema with tenant-specific prefix\n- Include all required tables (users, data, settings)\n- Handle errors and rollback on failure\n- Make script idempotent\n\n## Technical Approach\n1. Use Alembic for schema migrations\n2. Accept tenant_id as parameter\n3. Create schema: tenant_{tenant_id}\n4. Apply all base migrations to new schema\n\n## Files to Modify\n- `src/infra/tenants/schema.py` - Main script\n\n## Acceptance Criteria\n- [ ] Script creates schema with all tables\n- [ ] Script is idempotent\n- [ ] Unit tests verify schema structure"
  }
]
```

## Docs Drift Guard

**FOR MAINTAINERS:** This section prevents documentation from drifting from validator code.

### Validator Enums (Source of Truth)

All allowed values come from `src/runtime/cognition/models.py`. When modifying enums, update this guide immediately.

| Enum | Location | Current Values |
|------|----------|----------------|
| `EpicStatus` | models.py:23 | `idea`, `planned`, `in_progress`, `done`, `cancelled` |
| `FeatureStatus` | models.py:32 | `backlog`, `planned`, `in_progress`, `done`, `cancelled` |
| `UserStoryStatus` | models.py:41 | `seed`, `ready`, `in_progress`, `done`, `cancelled` |
| `TaskStatus` | models.py:50 | `todo`, `in_progress`, `ready_for_review`, `changes_required`, `blocked`, `done`, `cancelled` |
| `Priority` | models.py:82 | `P0`, `P1`, `P2`, `P3` |
| `Size` | models.py:90 | `XS`, `S`, `M`, `L`, `XL` |
| `WorkType` | models.py:103 | `feature`, `bug`, `chore`, `spike`, `incident` |
| `Severity` | models.py:116 | `blocker`, `critical`, `major`, `minor`, `trivial` |
| `Environment` | models.py:129 | `dev`, `test`, `staging`, `prod`, `other` |
| `Source` | models.py:142 | `ci`, `monitoring`, `user_report`, `internal_test`, `code_review` |

### Validator Rules (Source of Truth)

Import validation logic: `src/runtime/web/api_routes/project.py:1632` (`_validate_import_item`)

**Import validator accepts (subset of backlog enums):**
- **Status:** `idea`, `backlog`, `todo`, `in_progress`, `blocked`, `done` (lines 1698)
- **Priority:** `P1`, `P2`, `P3`, `P4` (line 1710) - **NOTE: P0 excluded, P4 added**
- **Size:** `XS`, `S`, `M`, `L`, `XL` (line 1722)
- **Type:** `epic`, `feature`, `story`, `task` (line 1652)

**Not accepted (backlog-only):**
- Status: `seed`, `ready`, `planned`, `ready_for_review`, `changes_required`, `cancelled`
- Priority: `P0`

### Maintenance Checklist

When updating validator enums or rules:

1. ✅ Update enum definitions in `src/runtime/cognition/models.py`
2. ✅ Update validator logic in `src/runtime/web/api_routes/project.py:1632`
3. ✅ Update this "Docs Drift Guard" table with new values and line numbers
4. ✅ Update "Status Values", "Priority Values", "Size Values" sections in this guide
5. ✅ Update `import-example.json` if adding new required fields
6. ✅ Run import validator tests to ensure no regressions
7. ✅ Update Last Updated timestamp at bottom of this file

**Drift detection:** If users report validation errors for documented values, check:
1. Did validator code change without updating docs?
2. Did line numbers shift in models.py?
3. Did new status/priority values get added to backlog but not import validator?

## Getting Help

- Review template files in `.daemoncore/cognition/ideas/.templates/`
- Check existing items in Ideas Lab for examples
- Refer to full specification in `docs/specs/backlog/backlog-import.md`
- If docs and validator behaviour differ, **validator wins** - file a docs bug

---

**Version:** 1.1
**Last Updated:** 2026-01-15
