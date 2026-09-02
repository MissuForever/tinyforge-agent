---
name: behavior-preserving-refactor
description: Use when restructuring existing code without intentionally changing its externally observable behavior.
---

# Behavior-Preserving Refactor

## Invariant

The refactor is successful only when the supported behavior before and after the change is equivalent.
Treat formatting improvements, abstraction changes, module moves, and duplication removal as secondary to
preserving the contract.

## Workflow

1. Identify the public surface, important internal callers, and tests that constrain current behavior.
2. Run the most relevant existing tests to establish a passing baseline. If the baseline fails, separate
   that failure from the refactor and report it before proceeding.
3. Define the structural improvement and keep the edit within that boundary.
4. Make changes in reviewable increments; avoid combining cleanup with unrelated fixes or features.
5. Run focused tests after the final edit and the broader suite when shared code or public interfaces moved.
6. Inspect the final diff for accidental behavior, dependency, configuration, or generated-file changes.

## Evidence

Report the baseline and final commands, the preserved interface, the structural improvement, and any
remaining compatibility risk. A passing command from before the last edit is stale.

## Stop Conditions

Stop when behavior is not sufficiently characterized, the requested structure requires an API change, or
existing failures prevent a credible equivalence check. Ask for the behavior decision instead of labeling
an intentional change as a refactor.
