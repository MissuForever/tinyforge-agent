---
name: gui-change-verification
description: Use when changing a desktop GUI workflow, layout, widget state, or background-task interaction that needs behavioral and visual verification.
---

# GUI Change Verification

## Outcome

Deliver the requested interaction without freezing the UI, hiding essential controls, leaking sensitive
data, or breaking supported window sizes and display scaling.

## Workflow

1. Trace the user action through widgets, signals, background work, state updates, and error handling.
2. Follow the repository's existing GUI architecture and thread boundary; UI objects stay on the GUI thread.
3. Keep task controls, empty/loading/error/completed states, and cancellation behavior coherent.
4. Add focused widget tests for state transitions and regressions. Use an offscreen platform only for tests
   that do not require a real compositor or display.
5. Run the focused GUI tests after the final edit, then the relevant suite.
6. For layout or display changes, inspect the real rendered interface at representative window sizes and
   scaling when the environment permits; record any visual check that could not be performed.

## Review Checks

- Text and dynamic content remain readable without overlapping or resizing fixed controls unexpectedly.
- Long-running work does not block event processing.
- Stale background results cannot overwrite a newer user choice.
- Secrets and untrusted tool output retain their existing redaction and sanitization boundaries.

## Stop Conditions

Do not claim visual validation from unit tests alone. Stop and report the gap when the required GUI toolkit,
display environment, or representative state cannot be exercised.
