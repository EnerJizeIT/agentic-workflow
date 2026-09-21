# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, please **do not** open a public issue.

Instead, email the maintainer directly or open a private security advisory on GitHub:
[Report a vulnerability](https://github.com/EnerJizeIT/agentic-workflow/security/advisories/new)

## Scope

awf runs locally on your machine. The main security considerations:

- **Worker subprocesses** inherit your environment. Don't run awf in directories with secrets you don't want agents to read.
- **Dashboard HTTP server** binds to `127.0.0.1` only (not accessible from network).
- **Form HTTP server** binds to `127.0.0.1` only.
- **Git operations** use `subprocess.run` without `shell=True` (no shell injection).
- **File permissions**: `inputs/*.yaml` and worker logs are `chmod 0o600`.

## Known limitations

- No CSRF token on form server — relies on `127.0.0.1` binding + Origin/Referer check (A2).
- No authentication on dashboard (local-only).
- `OPENCODE_CONFIG_CONTENT` env var contains permission overrides (not API keys).
