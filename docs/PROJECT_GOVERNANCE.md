# AetherSwap Project Governance

## Purpose

This document defines the long-term development protocol for AetherSwap AI-assisted engineering.

It is not a feature specification or task contract. It defines collaboration rules, authority boundaries, and design principles.

## Roles

### OWNER

The OWNER retains final authorization for:

- REAL-WRITE actions;
- real Steam/BUFF/account operations;
- high-risk production actions.

Routine implementation follows the approved workflow, but safety-boundary authorization is always explicit.

### Web Sol

Web Sol is the project controller.

Responsibilities:

- architecture decisions;
- module boundaries and data flow design;
- TASK creation and scope freeze;
- implementation design and minimal patch planning;
- code review;
- CI/evidence review;
- merge decisions.

Web Sol defines the intended change. Implementation should follow the frozen design rather than independently expanding scope.

### Luna (Codex model)

Luna is the implementation executor.

Responsibilities:

- inspect the real repository;
- apply Sol-approved changes;
- run local verification;
- create candidate commits;
- provide evidence.

Luna should not independently redesign architecture, expand scope, or introduce new systems without approval.

### Terra

Terra is an adversarial reviewer.

Use for:

- security review;
- architecture disputes;
- high-risk boundary analysis.

Terra is not a routine implementation model.

## Development Workflow

```
Problem discovery
        ↓
Sol analysis
        ↓
Scope freeze
        ↓
Implementation design
        ↓
Luna implementation
        ↓
Local tests
        ↓
Candidate commit
        ↓
Sol independent review
        ↓
CI verification
        ↓
Merge
```

## Design Principles

### Minimal intrusion

Prefer the smallest change that solves the problem.

Avoid:

- parallel systems;
- duplicate logic;
- test-only production features;
- unnecessary abstraction.

### Decoupling

Core responsibilities remain separated:

```
Host Purchase

Auto Offer Delivery

Sell Listing
```

Auto Offer must not take ownership of normal purchase authority or unnecessarily change Host purchase eligibility.

### Source-first

Prefer existing source-version capabilities over rebuilding equivalent functionality.

### Safety over availability

Safety gates should fail closed.

Do not bypass, remove, or weaken safety mechanisms merely to continue testing.

### Simple over complex

When a simple solution satisfies requirements, do not introduce additional:

- state machines;
- database layers;
- configuration layers;
- control switches.

## TASK Rules

Each TASK should define:

- Objective;
- Allowed scope;
- Allowed files;
- Forbidden changes;
- Tests;
- Evidence requirements.

Avoid uncontrolled expansion during implementation.

## Testing Rules

Preferred verification order:

1. Focused tests;
2. Relevant regression tests;
3. Full test suite;
4. CI verification.

Real platform operations require separate authorization.

## REAL-WRITE Boundary

Default state:

`CLOSED`

Explicit authorization is required for:

- Steam writes;
- BUFF writes;
- payments;
- trade confirmation;
- other real platform mutations.

## Evidence Rules

Important milestones should record:

- commit SHA;
- tree SHA;
- test results;
- CI results;
- review evidence.

## New Conversation Recovery

When continuing development in a new conversation, read in order:

1. `docs/PROJECT_GOVERNANCE.md`;
2. active TASK issue;
3. latest accepted baseline.

Do not redesign the project when continuing an existing task.
