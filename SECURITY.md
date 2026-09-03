# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability-reporting flow for this repository:

<https://github.com/hyper486/iracing-ai-engineer/security/advisories/new>

Do not attach credentials, raw telemetry, `SessionInfo`, private replay files,
machine receipts, crash dumps, ETL traces, Tailscale/RustDesk identifiers, or
other personal data to a public issue. Describe the minimum reproducible case
and provide sensitive evidence only through the private report.

## Safety boundary

This is experimental advisor-only software. The accepted design does not send
steering, throttle, brake, clutch, shift, simulator-launch, or iRacing pit-box
commands. A change that introduces any such executable path is a security and
acceptance-boundary violation and should be reported privately.

Only the current `main` branch receives security fixes. Historical deployment
receipts are evidence records, not supported standalone releases.
