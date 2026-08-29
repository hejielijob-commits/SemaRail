# Security Policy

## Supported versions

This project is currently in alpha. Security fixes are applied to the latest commit on `main`; older commits and local forks are not separately supported.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue, discussion, pull request, or log attachment.

Use GitHub's private vulnerability reporting for this repository from the **Security** tab. If that option is unavailable, contact the maintainer privately through the contact method on the maintainer's GitHub profile and include only enough information to establish a secure follow-up channel.

Please include:

- The affected version or commit.
- Reproduction steps or a minimal proof of concept.
- The expected and observed security boundaries.
- Potential impact and any known workarounds.

You should receive an acknowledgement within seven days. Please allow time for validation and a coordinated fix before public disclosure.

## Security assumptions

- Treat every model-generated SQL statement as untrusted.
- Run database queries with a dedicated read-only account and a minimal object allowlist.
- Keep database credentials in environment variables or a secret manager, never in repository files or Harness configuration.
- The Semantic Console binds to loopback and has no authentication in the alpha release. Do not expose it directly to a network or the public internet.
- Remove credentials and private business data from bug reports, screenshots, fixtures, and logs.
