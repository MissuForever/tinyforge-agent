# Python Verification

Prefer the repository's documented test command. Check `pyproject.toml`, `pytest.ini`, `tox.ini`, CI
configuration, and the tests package before choosing a fallback.

- For standard-library suites, use `python -m unittest discover -s tests -v` and preserve any required
  top-level argument such as `-t .`.
- For pytest projects, use the narrowest failing node first, then the relevant package or full suite.
- Run commands from the repository root unless project configuration says otherwise.
- Treat only a zero exit code with the expected tests collected as successful verification.
- Repeat verification after the last implementation edit; an earlier passing run is stale evidence.
