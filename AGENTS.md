# Repository working agreement

These instructions apply to the entire repository.

## Public-repository guardrails

- Treat every committed object and every pushed ref as public.
- Never commit credentials, authentication material, private keys, raw iRacing
  telemetry, private `SessionInfo`, local replay files, user-specific state,
  machine receipts, EAC/WPR traces, crash dumps, or remote-access identifiers.
- Use public-safe placeholders in examples and tests: `C:\Users\racer`,
  `/Users/racer`, `racer@aeis.example.invalid`, and RFC 5737 documentation IP
  addresses. Do not add real Windows/macOS usernames, Tailscale hostnames, or
  routable/private operator endpoints.
- Keep advisor-only safety invariant: no steering, throttle, brake, clutch,
  shifting, simulator launch, or pit-black-box mutation may be introduced.
- A historical deployment receipt may describe an external frozen artifact,
  but checked-in source must not claim byte identity with that artifact after
  public-safety sanitization.

## Milestone publishing

A major milestone includes a new user-visible engineering capability, a change
to the goal-acceptance state, a deployment or recovery boundary, a security or
privacy fix, or a tagged release.

Before publishing a major milestone:

1. Update the relevant status and handoff documentation.
2. Run `uv run ruff check .`.
3. Run `uv run pytest -q`.
4. Run `uv run python scripts/check_public_safety.py --include-history`.
5. Review `git diff --check` and the exact staged file list.
6. Commit with the repository's GitHub noreply identity and push the validated
   milestone to `origin/main`.

Do not push known-broken intermediate work. After the initial sanitized public
history is established, do not rewrite public `main` except for an emergency
credential or privacy purge.
