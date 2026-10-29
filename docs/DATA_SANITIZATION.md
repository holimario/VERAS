# Data Sanitization and Redaction

This document describes the data sanitization applied to the published artifact.

Removed material includes personal names and email addresses, local absolute
paths, repository account metadata, API credentials, local Codex databases,
session identifiers, solver licenses, and confidential operational datasets.

Retained benchmark trajectories are sanitized before inclusion in the public
artifact. Transport/API failures are removed rather than displayed, and API
endpoints, provider metadata, credential-source names, host paths, account
identifiers, and session identifiers are not retained. Generic user-selected
environment-variable names may appear in runner source, but no credential
values are stored.

Run `scripts/check_release_content.sh` before every public push.
