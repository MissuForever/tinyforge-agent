---
name: verified-bugfix
description: Use when fixing a reproducible defect while preserving existing tests and proving the result with before-and-after command evidence.
---

# Verified Bugfix

## When To Apply

Use this Skill only when the defect can be reproduced with an observable test or check and the requested
work allows implementation changes. Do not use it for speculative cleanup or a feature with no failing
baseline.

## Principle

Change the smallest implementation surface supported by the failing evidence, then prove the correction
with a fresh targeted check and relevant regression coverage.

## Procedure

1. Read the requirement, relevant implementation, and complete tests that define the behavior.
2. Run a focused command that reproduces the defect; retain its exit status and failure summary.
3. Localize the earliest actionable cause and make a focused implementation change. Preserve working
   behavior and existing tests.
4. After the final edit, repeat the same focused command, then run the broader relevant suite when the
   focused check does not cover likely regressions.
5. Report changed files, exact verification commands, and outcomes.

If the project is Python and its test entry point is unclear, read `references/python-verification.md`.

## Qualification

- The targeted check must fail before the change and pass after the final edit.
- Relevant existing regression checks must not get worse.
- Verification from before the last implementation edit is stale and cannot qualify the result.

## Stop Conditions

Stop and report the evidence instead of guessing when the defect cannot be reproduced, the requirement
conflicts with the tests, or the needed verifier is unavailable.

## Negative Example

Do not weaken, delete, or rewrite a correct test merely to obtain a green run. That hides the observed
failure instead of correcting its cause; change a test only when the user requests it or the requirement
demonstrates that the test itself is wrong.
