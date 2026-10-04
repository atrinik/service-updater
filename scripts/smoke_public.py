#!/usr/bin/env python3
"""Anonymous real OCI attestation verification; never starts a game container."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('updater', Path(__file__).resolve().parents[1] / 'adapters/classic.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
parser = argparse.ArgumentParser()
parser.add_argument('image')
parser.add_argument('--local-image', action='store_true')
args = parser.parse_args()
if args.local_image:
    if not re.fullmatch('sha256:' + d.HEX, args.image):
        raise SystemExit('local smoke image must be an immutable local image ID')
    # The production argument builder still validates a published immutable pin;
    # substitute only that exact image argument in this explicit CI harness.
    d.VERIFIER_IMAGE = 'ghcr.io/atrinik/service-updater@sha256:' + '0' * 64
    original = d.verifier_args
    def local_args(*a, **kw):
        return [args.image if item == d.VERIFIER_IMAGE else item for item in original(*a, **kw)]
    d.verifier_args = local_args
else:
    d.VERIFIER_IMAGE = args.image
release = d.api('/releases/latest')
tag = d.validate_release(release)
commit = d.api('/git/ref/tags/' + tag)['object']
for _ in range(4):
    if commit['type'] == 'commit':
        break
    d.need(commit['type'] == 'tag', 'unexpected tag type')
    commit = d.api('/git/tags/' + commit['sha'])['object']
d.need(commit['type'] == 'commit', 'tag nesting too deep')
digest, raw = d.manifest(tag[1:])
child_digest = d.amd64_child(raw)
_, child_raw = d.manifest(child_digest)
config_digest = json.loads(child_raw).get('config', {}).get('digest')
d.need(isinstance(config_digest, str) and re.fullmatch('sha256:' + d.HEX, config_digest), 'invalid child config digest')
bundles = d.api('/attestations/' + digest + '?per_page=30')
with patch.object(d, 'api', return_value=bundles):
    d.verify_index(raw, commit['sha'])
    for name, artifact, revision, workflow in (
        ('changed artifact', raw + b' ', commit['sha'], d.SIGNER_WORKFLOW),
        ('wrong source', raw, '0' * 40, d.SIGNER_WORKFLOW),
        ('wrong workflow', raw, commit['sha'], d.REPO + '/.github/workflows/not-the-publisher.yml'),
    ):
        try:
            d.verify_index(artifact, revision, workflow)
        except d.Rejected:
            print(name + ': rejected')
        else:
            raise SystemExit(name + ': unexpectedly accepted')
print(json.dumps({'release': tag, 'index': digest, 'child': child_digest, 'config': config_digest, 'source': commit['sha'], 'anonymous_offline_verification': 'passed'}))
