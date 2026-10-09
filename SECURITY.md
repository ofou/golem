# Security

## Supported versions

The newest `v1.x` release. Fixes ship as new `v1.x` releases, and the `v1` tag follows the newest one.

## Reporting a vulnerability

Report it privately through [GitHub private vulnerability reporting](https://github.com/ofou/golem/security/advisories/new), not in a public issue.

In scope:

- An OpenRouter key reaching a log, step summary, artifact, cache entry, comment, or sandbox container.
- A way out of the sandbox: network access, a write, the host environment, or code running outside the container.
- Starting a run that holds the key without write access to the repository, through a `/golem` comment, a pull request from a fork, or any other event.
- A run trusting a licence or a registry that came from a pull request.
