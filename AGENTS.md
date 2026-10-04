# Atrinik service updater

This repository owns the MIT generic updater core and reviewed service adapters.
Classic source/binaries/data remain in their own repositories and deployments.
Keep host identities, credentials, saves and private evidence out of commits.

Use an owned native Linux Git worktree; preserve unrelated changes. Source and
fixture work grants no deployment, credential, merge or cleanup authority.
Never build server binaries on deployment hosts. Package builds run in CI.

Keep independent review for implementation, especially lock, fence, provenance,
durable-save, backup/recovery and irreversible-boundary changes. Run
`python3 -m unittest discover -s tests -v`, `python3 scripts/package.py 0.0.0`,
syntax/whitespace checks and relevant image smoke tests. Run actionlint for
workflow changes. Pin image digests, keep verification offline and credential-free,
and leave activation disabled by default.

Only semantic-release publishes versions. No manual version tags, mutable image
overwrite or unreviewed runtime adapters. Add services through reviewed code and
strict configuration, never arbitrary configured shell hooks.
