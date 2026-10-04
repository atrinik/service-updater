# Release contract

The MIT `atrinik/service-updater` repository publishes the Linux amd64 verifier
and service updater sources as `ghcr.io/atrinik/service-updater:VERSION` and
`atrinik-service-updater-VERSION.tar.gz`. The manifest records the immutable OCI
index digest. Version tags are convenience selectors; consumers pin the digest.
The updater is separate from the GPL Classic server it verifies.

`Updater validation` and `Conventional PR title` are the stable merge checks.
The validation aggregate requires Python 3.11/3.14 tests, packaging, public
Classic provenance positive/negative smoke through the isolated container, and
a disposable local-Git test of the pinned semantic-release engine contract.
The npm dependencies only run release tooling; they are not updater dependencies.

`release.yml` runs on protected `main` or an explicitly requested dispatch from
`main`. It requires the latest successful GitHub Actions `Updater validation`
for the exact current main commit. Jobs use GitHub-hosted Ubuntu runners;
release builds and tooling never run on an application host.

Semantic-release determines versions from Conventional Commits, starts at
`1.0.0` for the first release, and alone creates Git tags. No fixed next release
version is committed. The private npm package's `0.0.0` is a tooling placeholder.
There is no npm publication, release commit, or automated issue/PR comment.

The publication order is:

1. Plan the version and reject any incompatible pending release transaction.
2. Package deterministic source bytes. Reuse a versioned image only after its
   original GitHub attestation proves the repository, release workflow, exact
   source SHA and hosted-runner identity; otherwise build the absent image.
3. Attest the new image index, run credential-free verifier checks against
   public Classic fixtures, attest the archive, and verify both attestations.
4. Retain the manifest and source archive as a 30-day Actions candidate artifact.
5. In semantic-release's `prepare` hook, create a draft tied to the exact source
   commit, upload missing assets, and compare the exact asset set, byte sizes
   and GitHub-computed SHA-256 hashes. Existing asset bytes are never replaced.
6. Let semantic-release create its Git tag. Recheck remote tag identity, source
   freshness, image digest/provenance and every asset before publishing the
   complete draft. Published releases are never modified by this automation.

The draft contains exactly the versioned archive, `SHA256SUMS` and
`release-manifest.json`. GitHub stores the archive and image attestations; the
image index also carries BuildKit provenance and an SBOM. Artifact storage
metadata is disabled because it is not required for provenance verification.

## Recovery

Rerun the release workflow on the same current main commit after transient
failures. A partially uploaded draft resumes only missing matching assets. If
semantic-release already pushed its tag, the complete draft is finalized
without creating, moving or deleting any tag. A lost successful publication
response is a verified no-op. Existing version images are never rebuilt or
replaced.

Failures between image push and its first valid GitHub attestation fail closed:
the image cannot be reused merely because its labels look correct. Preserve its
digest and failed-run evidence for explicitly reviewed recovery. Likewise, an
orphan tag, foreign draft, unexpected asset, or main advancing while a draft is
pending blocks publication. Recovery of an older source requires a separately
reviewed procedure; do not delete drafts/tags, overwrite images, reset main, or
advance to another version to bypass the pending transaction. Candidate
artifacts expire after 30 days; this is not a permanent backup mechanism.

A newly created GHCR package can start private even when its repository is
public. Registry authentication exists only on the GitHub-hosted build runner;
the verifier receives no token, Docker socket or host configuration. Package
visibility is an owner setting, outside this workflow. After initial publication,
verify the exact repository association and public package visibility, then
prove anonymous access using a new empty Docker configuration and the manifest's
exact image digest. Public Classic fixture verification does not prove this
new package's anonymous availability.

Register the repository, required contexts, semantic-release policy and release
tag protection in `atrinik/github-settings`. All actions are immutable pinned
references and fit its existing selected-actions allowlist. Enable the separate
organization immutable-release policy only after this workflow is merged and
proven; source readiness alone does not satisfy that policy's activation gate.

Local checks:

```sh
npm ci --ignore-scripts --no-audit --no-fund
python3 -m unittest discover -s .github/release -p 'test_*.py' -v
node .github/release/test_semantic.mjs
actionlint .github/workflows/*.yml
git diff --check
```
