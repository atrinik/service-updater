# Adapter contract

`core.ReleaseSource(repository, image, signer_workflow)` supports public GitHub
release metadata and GHCR OCI images independently of service implementation.
It bounds responses, refuses redirects, calculates digests over exact received
manifest bytes and resolves one Linux/amd64 child. The core validates stable
publication metadata, immutable pins, monotonically accepted versions,
cryptographic source/workflow provenance and durable filesystem records.

`core.DevelopmentSource(repository, image, signer_workflow, source_ref,
discovery_tag)` adds opt-in OCI source publications. Reviewed adapters choose the
canonical tuple. Pins carry `release_channel: development`, version `0.0.0`,
a full source revision and the same immutable image graph; they never carry a
GitHub release ID. Discovery checks both the mutable alias and `source-REVISION`
tag against the captured index. Every accepted source must be an ancestor of
the candidate; reused source revisions require an identical complete pin.
Stable and development records cannot share a ledger. No service-specific
repository, image or workflow is embedded in this helper.

`core.activate(adapter, candidate, lock)` is the shared transaction. Production
callers hold the service's root-owned flock for the entire operation. A single
handoff unlocks around systemd's stop hook after a durable drain journal, then
relocks and revalidates journal and fences. Service starts/stops use that same
lock. `ClassicAdapter` maps the interface to the Classic implementation. Tests
use a synthetic adapter with real durable records.

Adapters provide protected `ROOT`, `SERVICE` and `CONTAINER` identities and:

- `fence`, `load`, `atomic`, `command`, `check_monotonic`, `inspect_image`,
  `verify_provenance`, `capability`, `compatibility`, `headroom`: prove configured
  host, complete cohort, trust, release and capacity before effects.
- `drain`: application warning/drain with checked durable save and clean exit
  from the original runtime; `stopped` proves that it stopped.
- `guard`: close ingress, or open only after the durable boundary.
- `archive`: invoke `core.archive_files` on complete stopped state/config,
  deployment records and exact prior image, then write a checked manifest.
- `clone_check`: test only an isolated copy with no public access, validate
  compatibility/persistence and retain uncertain evidence.
- `launch`, `healthy`, `require_control`, `validate_access`, `validate_runtime`,
  `identity`: prove the candidate live cohort ready behind closed access.
- `stop_isolated`, `restore`: usable only before public access may have opened;
  preserve failed state, restore the entire old cohort and leave it stopped.
- `retain`: delete only verified completed archives under the retention policy.

Journal phases are `draining`, `drained`, `testing`, `closed`, `may-have-played`
and `rollback-review`. The historically named `may-have-played` means any new
external service writes may have occurred. An adapter must never automatically
restore stale state at or after that boundary.

Only Classic is registered. Next-generation server, bot or other service support
requires reviewed adapter source, explicit dispatch registration, strict schema
and tests for application save/drain, complete recovery, isolation and boundary
failures. Configuration cannot load Python modules, execute arbitrary commands
or disable fences.
