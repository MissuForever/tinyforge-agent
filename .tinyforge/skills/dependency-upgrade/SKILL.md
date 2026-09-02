---
name: dependency-upgrade
description: Use when adding or upgrading a project dependency while preserving reproducible installation and checking compatibility.
---

# Dependency Upgrade

## Principle

Change dependency declarations and generated lock state as one compatibility decision. Do not upgrade
unrelated packages merely because a package manager offers newer versions.

## Workflow

1. Locate every authoritative declaration, lock file, runtime constraint, optional group, and CI install path.
2. Determine the requested version range and inspect relevant release or migration notes when available.
3. Update only the declarations and lock entries required for the requested dependency.
4. Confirm a clean dependency resolution or installation using the repository's package manager.
5. Run focused compatibility tests and the relevant regression suite after the final dependency change.
6. Review the resolved diff for unexpected transitive upgrades, platform changes, or removed integrity data.

## Evidence

Report the requested and resolved versions, changed manifests or lock files, install or resolution command,
test commands, and any upstream compatibility caveat that remains.

## Stop Conditions

Stop instead of guessing when the registry is unavailable, integrity verification fails, platform-specific
resolution cannot be reproduced, or the upgrade requires an unrequested breaking API migration.
