# Atrinik service updater

An MIT-licensed controller for updating stateful Atrinik services from published,
attested container releases. The reusable core provides release discovery,
immutable OCI verification, durable records, complete-cohort archive primitives
and a fail-closed update transaction. The first supported adapter is the Classic
development server. Other services require a reviewed adapter with lifecycle
acceptance; arbitrary shell hooks are not supported.

The primary package is `ghcr.io/atrinik/service-updater`. Its image contains the
Python host controller, adapters, configuration schema, license and documentation
under `/updater`, plus a pinned GitHub CLI and verified Sigstore trusted roots.
A release also includes `atrinik-service-updater-VERSION.tar.gz` and its checksum.

## Runtime boundary

A small Python standard-library controller runs on the Linux host under root so
it can coordinate Docker, systemd, firewall guards and complete state backups.
The same updater image runs as an unprivileged, read-only offline verifier:
it gets only a read-only mount containing public OCI metadata and attestations,
no Docker socket, host PID/network namespace, home directory or credentials.
This is a container-distributed updater with a host integration component.

Deployment hosts need Python 3.10+, the ordinary Docker CLI, systemd, nftables,
GNU tar/coreutils, util-linux and OpenSSL. They do not need GitHub CLI, Buildx,
Node, a compiler, Git checkout, or server build dependencies. Server images are
always pulled from published releases; the controller has no server build path.
Package-image builds run in GitHub Actions.

## Deployment

Use the deployment repository to verify a published updater release on a trusted
workstation, record its immutable image digest, pull that digest on the target,
and extract `/updater` with `docker create`, `docker cp`, and `docker rm` of the
created container. Extraction does not start a container. Install the controller
root-owned in its own protected directory and preserve its license.

Create a root-owned mode-0600 deployment file from
[`config/deployment.example.json`](config/deployment.example.json). Placeholder
identities intentionally fail validation. Establish all machine, filesystem,
disk, production identity, unit and path fences from current inventory. No
private host identities are included here. The schema is
[`config/deployment.schema.json`](config/deployment.schema.json); semantic checks
also reject overlapping state/backup roots and service identities.

The Classic adapter additionally requires the protected pin, accepted-release
ledger, identity and real private-map acceptance records described in
[Classic integration](docs/classic.md). Copy `policy.example.json` with activation
false. Deployment owns the systemd services/timer and firewall rules, initially
stopped and disabled. No install command here enables them.

```sh
python3 /opt/service-updater/service_updater.py --config /etc/atrinik/updater.json check
```

Updater-package upgrades are explicit deployment changes under the same lock,
with services/timer stopped and the prior package retained. Runtime polling
updates service releases; it does not replace its own trusted controller/image.

## Safety and extension

The adapter proves host/state identity, image provenance, compatibility and
capacity before requesting a checked application drain. For Classic this means
an authenticated Unix-socket in-game countdown and durable saved receipt from
the exact container, with no live signal fallback.

The controller closes access, archives the stopped complete cohort and prior
image to a separate filesystem, fsyncs before its receipt, and tests an isolated
clone. It records an irreversible boundary before opening access. Once new
writes may have occurred, automatic stale restoration is forbidden. An
interrupted/failed transaction remains fenced for operator recovery.

Read [the adapter contract](docs/adapters.md), [provenance](docs/provenance.md),
and [Classic integration](docs/classic.md) before extending or operating it.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 scripts/package.py 0.0.0
```

CI packages the updater image and runs
`python3 scripts/smoke_public.py IMAGE_ID --local-image`. This verifies a real
published Classic OCI index and rejects changed bytes, a wrong source commit,
and a wrong signer workflow without login or network inside the verifier.
It does not start a game server. Live Classic activation requires an accepted
published release supporting the countdown/save interface and actual private-map
acceptance.

Releases are published by the reviewed semantic-release workflow; see
[release lifecycle](https://github.com/atrinik/service-updater/blob/main/.github/release/README.md). Do not create
version tags, images or release assets by hand. This project uses the
[MIT license](LICENSE). Server binaries and data are not distributed here.
