---
name: security-code-review
description: Use when reviewing code specifically for trust-boundary, credential, command-execution, path, or untrusted-input vulnerabilities.
---

# Security Code Review

## Scope

Review the requested code and its immediate callers as an evidence-based assessment. This Skill is read-only
unless the user also asks for fixes. Ordinary code review without a security focus should not load it.

## Review Method

1. Identify assets, untrusted inputs, privilege boundaries, external effects, and sensitive outputs.
2. Trace data through parsing, validation, canonicalization, authorization, execution, persistence, logging,
   and presentation. Check where validation occurs relative to the effect.
3. Look for concrete exploit paths involving command or code execution, path traversal and links, secret
   disclosure, unsafe deserialization, injection, confused-deputy behavior, race conditions, and fail-open handling.
4. Verify whether existing defenses cover encoded, split, oversized, concurrent, and platform-specific inputs.
5. Report only findings supported by a reachable path or clearly state the assumption needed for reachability.

## Findings Format

Order findings by severity. For each finding, provide the affected file and line, triggering input or state,
impact, why the current defense is insufficient, and a focused remediation. Distinguish vulnerabilities from
hardening suggestions and list residual test gaps when no vulnerability is found.

## Boundaries

Do not expose real credentials in examples or logs, run destructive proof-of-concept commands, or claim that
rule-based validation is a complete sandbox. Do not modify code during a review-only request.
