# License and provenance

The controller and tests were authored for an Atrinik development deployment and
extracted into this standalone project with the owner's explicit MIT license
authorization. The generic core and packaging code are new project work. No
Classic C/CMake implementation, GPL server source, server executable, private
configuration, save data, certificates or logs are copied into this repository
or image. Interacting with the native admin interface does not copy its
implementation. Preserve third-party notices; the image carries GitHub CLI's
MIT license separately.

The host fetches bounded public GitHub release/tag metadata and GHCR manifest
bytes anonymously. It hashes exact bytes, selects the immutable Linux/amd64 child
and verifies its index/child/config hash chain and engine identity. Image source/version/revision labels
must match. GitHub's public REST attestations endpoint supplies signed bundles.
Missing bundles, mismatched provenance or API failure rejects the operation
before draining the service.

The digest-pinned updater image runs `gh attestation verify` on the raw index
with `--bundle`, `--custom-trusted-root`, `--repo`, `--signer-workflow` and
`--source-digest`; a successful nonempty result is required. The verifier has
no network or host credentials. This public-bundle path avoids the login needed
by default GitHub CLI online attestation lookup.

The Dockerfile pins the official GitHub CLI 2.102.0 Linux/amd64 archive SHA-256
and its build-stage base image. At build time `gh attestation trusted-root`
uses the pinned CLI's embedded TUF trust anchors to authenticate current
Sigstore roots; invalid metadata fails packaging. These authenticated roots are
copied into the final image. No configurable trust-root URL or runtime
trust-anchor replacement exists. Updating CLI/base/trust snapshot requires a
reviewed updater release, whose immutable package digest deployment records.
Verify the updater release independently on a trusted workstation against its
publisher identity before installing it.

Public integration tests prove offline verification and rejection of changed
bytes, wrong source and wrong workflow. Release workflows attest package image
and source archive and use immutable actions with least-privilege permissions.

References: [verification](https://cli.github.com/manual/gh_attestation_verify),
[authenticated trust export](https://cli.github.com/manual/gh_attestation_trusted-root),
[attestation REST API](https://docs.github.com/en/rest/users/attestations).
