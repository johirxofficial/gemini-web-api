# Security Policy

## Supported versions

The latest release on `main` is supported with security updates.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting:

<https://github.com/johirxofficial/gemini-web-api/security/advisories/new>

Include:

- A short description of the issue
- Steps to reproduce
- Affected version or commit
- Any suggested fix

You can expect an initial response within a few days.

## Local security notes

- This project controls a real browser profile. Treat that profile like a
  password.
- `config.yaml`, `.venv/`, `logs/`, and browser profile folders are ignored by
  git for a reason. Do not commit them.
- The API binds to `127.0.0.1` by default. If you expose it, add
  authentication, HTTPS, and a reverse proxy.
- Set `api.api_key` before using the API on a shared machine or network.
- Prompts are sent to Gemini through your own browser session and are subject
  to Google's terms and privacy policy.
