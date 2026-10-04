# Classic development integration

The Classic adapter uses official `atrinik/classic` releases,
`ghcr.io/atrinik/classic-server`, and the signer workflow
`atrinik/classic/.github/workflows/package-release.yml`. These are checked by its
strict schema. Runtime containers must be Linux/amd64. Docker containerd
manifest descriptors must match the selected child; an index-backed runtime
without a child descriptor is rejected, while legacy config-backed runtimes
remain supported. Other service sources belong to other reviewed adapters.

Deployment configuration must establish the machine ID, state filesystem UUID,
disk serial, separate backup filesystem, stopped production unit/container,
loaded production start fence, migration marker and production certificate
fingerprint. Development state and identity must differ from production. Unit,
lock, admin socket, state and deployment paths are mandatory configuration.
The loaded production fence is checked through the system manager's D-Bus API
using the installed `busctl` utility. The controller resolves the configured
unit with `GetUnit`, checks its exact canonical `Id`, then requires a typed
`Conditions` tuple for non-trigger, negated `ConditionPathExists` with the exact
migration-marker path. A missing/aliased unit, empty conditions, wrong tuple,
malformed output or query failure is rejected. The historical evaluation state
may be zero (not evaluated); it does not replace the configured condition.
This avoids systemd versions that render `systemctl show Conditions` as
`[unprintable]`, while still requiring both the on-disk fence and loaded condition.
See the [systemd D-Bus Conditions contract](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html).

Deployment installs its reviewed nftables rules as `closed.nft` and `public.nft`
under the configured root. Both rules must retain the deployment's other fences;
closed access must include loopback to prevent candidate writes before acceptance.

The protected state cohort contains `server-data`, including its initialization
marker, complete accounts/players/private maps and development QUIC certificate,
plus `config/server-custom.cfg`. The adapter requires an explicit
`[meta] access_required=true` (protected) or `false` (open), independently of
individual credentials. It forbids publishing a direct endpoint and retains the
development certificate identity. Legacy `join_password`, `join_password_file`
and `rendezvous_invite_file` settings, and the old invitation file, are rejected;
transition requires explicit offline migration before this adapter can run.
Account authentication and player saves are separate and remain unchanged.

Configuration uses a conservative subset of the native line-oriented grammar,
not INI parsing. Use unique lowercase `[section]` labels and globally unique
lowercase `name=value` assignments, with nonempty arguments. Controlled access
and endpoint options belong only in `[meta]` and must be unquoted. Spaces or tabs
around `=` and LF/CRLF line endings are supported; active-line indentation,
continuations, uppercase keys, colon assignments, duplicate sections/options,
abbreviations of controlled options or `config`, recursive `config` directives,
file indirection, escapes
and control characters are rejected. Comments begin with `#` at column one.
Omit optional unset settings rather than supplying blank values. Files are
bounded to 1 MiB, physical lines to 4094 UTF-8 bytes, and normalized `name=value`
to 4093 bytes so the native 4096-byte input/argument buffers cannot truncate an
accepted option. Unsupported forms fail before native status execution.

The canonical token directory is `server-data/access-tokens`, owned by UID/GID
10001 with mode 0700. Its sole mode-0600 file, `access-tokens.snapshot`, contains
all grants, audit history, removal tombstones, mutation receipts, route outbox
and commit state. Omit `access_store` or use the container path
`/opt/atrinik/server/data/access-tokens`; external stores are forbidden.
`access_initialize` must be absent or `false`: the updater never bootstraps,
issues, renews, revokes or removes a credential.

In-game token administrators may be configured through
`access_admin_accounts=/opt/atrinik/server/access-admin-accounts`. This maps to
`config/access-admin-accounts` in the cohort, owned by root with group 10001 and
mode 0440 or 0640, and mounted read-only. Omit the setting when no allowlist is
configured; an empty allowlist file grants nobody access. Do not grant authority
through inherited player groups. Optional path settings cannot be blank.

Store health comes from the native `access-tokens-v1` status contract. Running
services answer an authenticated root Unix-socket request with a bounded,
length-prefixed JSON response. Stopped services use the published image's
`/opt/atrinik/server/atrinik-access-status` command in a network-isolated,
read-only container with only the read-only data-directory mount. Its arguments
are `--data-dir`, `--store-dir`, `--certificate` and `--policy`; none contains a
credential. The native inspector must acquire the existing data directory's
exclusive lock and validate without startup, initialization, reconciliation,
networking or writes. Image provenance is checked before executing it.

Both paths require the exact supported status schema, certificate identity,
configured policy, integrity and durability. `pendingRouteSync` counts retained
per-token route work and must be an integer from 0 through 1024; the separate
32-item dispatch limit does not make a larger durable backlog unhealthy.
An initialized protected store is
valid when empty, revoked or fully expired. An absent store is valid only for
explicit open policy. The updater never reads token expiry or fabricates use
history. Duplicate or unknown JSON fields, response/request mismatch, malformed
framing and unavailable native status fail closed; there is no legacy fallback.

The configured root holds these root-owned mode-0600 records:

- `pin.json`: stable version, immutable child `image`, attested `index_image`,
  OCI `config_digest`, engine `image_id` bound to index/child/config, 40-digit source `revision` and positive
  numeric GitHub `release_id`.
- `ledger.json`: `accepted`, an ordered list of complete pin records ending at
  the current pin; changed/downgraded accepted releases are rejected.
- `identity.json`: `sha256`, the development certificate DER fingerprint.
- `acceptance.json`: `private_map_roundtrip: true`, `private_map_count` of at
  least five, accepted `minimum_version`, and 40-digit `source_revision`.
  Create only after real restored-map load/save/logout/relogin acceptance.
  Later release revisions must descend from that accepted source revision.
- `policy.json`: `activation_enabled`, initially false. Only Boolean true enables
  update activation; strings/numbers do not.

Use `service_updater.py --config ABSOLUTE_CONFIG ACTION`. Actions are `check`,
`stage`, `update`, `run`, `stop` and `close`. All update/runtime mutations use one
configured flock. Deployment owns services and timer and leaves them stopped
and disabled until acceptance. The controller never seeds a private cohort.

Before a live update, the adapter authenticates native Unix socket peer PID/UID,
requires countdown and durable-result capabilities, requests a 60-second player
warning, and accepts only the exact saved receipt plus clean container exit.
Capabilities form a bounded unordered set: at most 32 distinct ASCII tokens of
1–48 lowercase letters, digits or hyphens in a line of at most 2048 bytes.
Unknown well-formed capabilities are tolerated; `shutdown-v1` and
`durable-result-v1` remain required. Access validation additionally requires
`access-tokens-v1`. The native save result must cover token/audit/outbox state
as well as game saves.
There is no live SIGTERM/SIGKILL fallback. After shutdown it closes ingress,
archives the complete state/config/records and old image, verifies the archives,
and fsyncs files/directories before recording a manifest.

An isolated clone runs without network and must shut down through the same
checked save interface. Account/player/private-map file sets and byte hashes
must remain unchanged in this idle check; quarantine is rejected. The complete
authorization snapshot, allowlist, configuration and certificate are fingerprinted
in the backup manifest and must also remain unchanged in the isolated clone.
No clone publishes routes, admits a real player or supplies an operator code. The live
candidate starts behind closed ingress, then must pass runtime mounts, identity,
health, access-store status and control checks. The accepted ledger and irreversible
transaction boundary are durable before ingress opens.

Pre-boundary failure may restore the complete previous cohort while retaining
failed saves; it stays stopped for review. Before restoring anything, the
adapter compares current authorization state with the archived fingerprint. A
newer revocation, audit/outbox change, policy or identity change blocks automatic
restoration and preserves the closed failed cohort for explicit coherent recovery.
An old backup without that fingerprint is also rejected. Post-boundary automatic restoration
is forbidden. Interrupted transactions, failed archives and candidate clones
remain for investigation. Never delete a transaction to force a start. Retention
keeps the first verified completed backup and six newest completed backups;
failed/incomplete backups do not displace those slots.

Unit tests and an image health result do not replace real deployment acceptance:
a published native-interface image, real countdown/save evidence, all restored
private-map gameplay checks, and development discovery/access must pass first. In particular, the native access
inspector, initialized/absent-open status, checked token persistence, revocation
and audit preservation need producer/consumer integration acceptance against the
exact compatible published release. Mocked status fixtures do not prove native
store parsing or distributed revocation behavior.
