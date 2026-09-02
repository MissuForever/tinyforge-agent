---
name: tested-feature
description: Use when implementing a scoped feature whose expected behavior can be expressed and verified with automated tests.
---

# Tested Feature

## When To Apply

Use this Skill for a new capability or an intentional behavior change with a defined user-visible outcome.
Do not use it for a defect that already has a failing reproduction; use the verified bugfix workflow instead.

## Workflow

1. Read the request, nearby implementation, public interfaces, and complete relevant tests.
2. State the behavior to add, compatibility constraints, and likely affected callers before editing.
3. Add or update focused tests that distinguish the new behavior from the old behavior. Avoid assertions
   tied only to implementation details.
4. Implement the smallest coherent change that satisfies the behavior and follows local patterns.
5. Run the focused tests after the final edit, then run the relevant regression suite.
6. Report the observable behavior, changed files, commands, and outcomes.

## Qualification

- New behavior has direct automated coverage or an explicit reason why automation is unavailable.
- Existing behavior outside the requested change remains covered and passing.
- Verification must run after the final implementation or test edit.

## Stop Conditions

Stop and surface the decision when requirements conflict, the public contract is ambiguous in a way that
changes the result, or verification requires unavailable infrastructure. Do not silently choose a new API
or broaden the feature.
