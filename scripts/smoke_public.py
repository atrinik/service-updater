#!/usr/bin/env python3
"""Anonymous real OCI attestation verification; never starts a game container.

The public reads stay anonymous by design, so they share the runner address's
unauthenticated GitHub API rate limit. Only rate-limit responses are retried,
within a bounded total wait. Other HTTP errors and every rejection fail at once.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
from unittest.mock import patch

RATE_LIMIT_BUDGET = 300  # total seconds this smoke may wait for rate limits
RATE_LIMIT_RETRIES = 3  # per request
DEFAULT_RATE_LIMIT_WAIT = 60  # GitHub's advice when a limit names no usable time


class RateLimited(RuntimeError):
    pass


def header_seconds(headers, name):
    value = (headers.get(name) or '').strip()
    return int(value) if re.fullmatch(r'[0-9]{1,12}', value) else None


def rate_limit_wait(error, now):
    """Seconds a 403/429 rate-limit response asks to wait, or None if not one."""
    headers = error.headers
    if error.code not in (403, 429) or headers is None:
        return None
    if headers.get('Retry-After') is not None:
        wait = header_seconds(headers, 'Retry-After')
    elif (headers.get('X-RateLimit-Remaining') or '').strip() == '0':
        reset = header_seconds(headers, 'X-RateLimit-Reset')
        wait = None if reset is None else reset - int(now) + 1
    else:
        return None
    return DEFAULT_RATE_LIMIT_WAIT if wait is None else max(1, wait)


def retry_rate_limits(fetch, budget=RATE_LIMIT_BUDGET, retries=RATE_LIMIT_RETRIES,
                      sleep=time.sleep, clock=time.time):
    """Wrap an anonymous fetch so only rate-limit responses wait and retry."""
    remaining = budget

    def fetch_with_retry(url, *args, **kwargs):
        nonlocal remaining
        for attempt in range(retries + 1):
            try:
                return fetch(url, *args, **kwargs)
            except urllib.error.HTTPError as error:
                wait = rate_limit_wait(error, clock())
                if wait is None:
                    raise
                error.close()
                if attempt == retries:
                    raise RateLimited(f'rate limited: {url} still returned HTTP {error.code} '
                                      f'after {retries} retries') from error
                if wait > remaining:
                    raise RateLimited(f'rate limited: {url} returned HTTP {error.code} and asks '
                                      f'for {wait}s, more than the {remaining}s left of the '
                                      f'{budget}s retry budget') from error
                print(f'rate limited: {url} returned HTTP {error.code}; retrying in {wait}s',
                      file=sys.stderr, flush=True)
                remaining -= wait
                sleep(wait)
    return fetch_with_retry


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('updater', root / 'adapters/classic.py')
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
    # Every GitHub API and GHCR read below goes through core.public_bytes.
    d.core.public_bytes = retry_rate_limits(d.core.public_bytes)
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


if __name__ == '__main__':
    try:
        main()
    except RateLimited as error:
        raise SystemExit(str(error)) from None
