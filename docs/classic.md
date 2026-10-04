# Classic development integration

The Classic adapter uses official `atrinik/classic` releases,
`ghcr.io/atrinik/classic-server`, and the signer workflow
`atrinik/classic/.github/workflows/package-release.yml`. These are checked by its
strict schema. Other service sources belong to other reviewed adapters.

Deployment configuration must establish the machine ID, state filesystem UUID,
disk serial, separate backup filesystem, stopped production unit/container,
loaded production start fence, migration marker and production certificate
fingerprint. Development state and identity must differ from production. Unit,
lock, admin socket, state and deployment paths are mandatory configuration.
Deployment installs its reviewed nftables rules as `closed.nft` and `public.nft`
under the configured root. Both rules must retain the deployment's other fences;
closed access must include loopback to prevent candidate writes before acceptance.

The protected state cohort contains `server-data`, including its initialization
marker, complete accounts/players/private maps and development QUIC certificate,
plus `config/server-custom.cfg`. The adapter requires a join password, forbids
publishing a direct endpoint, and validates the invitation's identity binding
and strict native seven-day lifetime. It never rotates credentials/invitations.

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
There is no live SIGTERM/SIGKILL fallback. After shutdown it closes ingress,
archives the complete state/config/records and old image, verifies the archives,
and fsyncs files/directories before recording a manifest.

An isolated clone runs without network and must shut down through the same
checked save interface. Account/player/private-map file sets and byte hashes
must remain unchanged in this idle check; quarantine is rejected. The live
candidate starts behind closed ingress, then must pass runtime mounts, identity,
health, invitation and control checks. The accepted ledger and irreversible
transaction boundary are durable before ingress opens.

Pre-boundary failure may restore the complete previous cohort while retaining
failed saves; it stays stopped for review. Post-boundary automatic restoration
is forbidden. Interrupted transactions, failed archives and candidate clones
remain for investigation. Never delete a transaction to force a start. Retention
keeps the first verified completed backup and six newest completed backups;
failed/incomplete backups do not displace those slots.

Unit tests and an image health result do not replace real deployment acceptance:
a published native-interface image, real countdown/save evidence, all restored
private-map gameplay checks, and development discovery/access must pass first.
