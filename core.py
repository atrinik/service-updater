"""Generic service-update primitives and the fail-closed transaction contract.

Adapters implement service-specific fencing, drain/save, clone, health and access
operations. The core never executes arbitrary configured shell hooks.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.request

HEX = r'[0-9a-f]{64}'

class Rejected(RuntimeError):
    pass


def need(ok, message):
    if not ok:
        raise Rejected(message)


def command(*args, input=None, timeout=120, allowed=(0,)):
    # Private temporary output avoids unbounded memory and never reaches logs.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run([str(x) for x in args], input=input, text=True,
                                stdout=stdout, stderr=stderr, timeout=timeout)
        need(result.returncode in allowed, 'command failed: ' + str(args[0]))
        need(stdout.tell() <= 8 * 1024 * 1024 and stderr.tell() <= 8 * 1024 * 1024, 'excessive command output')
        stdout.seek(0)
        return stdout.read().decode('utf-8').strip()


def atomic(path, obj):
    tmp = path.with_suffix(path.suffix + '.new')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(obj, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if tmp.exists():
            tmp.unlink()


def private(path, uid, gid=None, modes=(0o700,), directory=False):
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), 'unsafe path type')
    need(info.st_uid == uid and (gid is None or info.st_gid == gid), 'unsafe ownership')
    need(stat.S_IMODE(info.st_mode) in modes, 'unsafe permissions')
    need(path.resolve() == path, 'symlink in protected path')
    need(directory or info.st_nlink == 1, 'hard-linked protected file')


def load(path):
    private(path, 0, modes=(0o600,))
    return json.loads(path.read_text())


def version(value):
    need(isinstance(value, str) and re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value), 'invalid stable version')
    return tuple(int(x) for x in value.lstrip('v').split('.'))


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Rejected('HTTP redirect rejected')


def public_bytes(url, headers=None, limit=8 * 1024 * 1024):
    # A fresh opener cannot inherit cookies or GitHub/Docker credentials. Tokens
    # used for GHCR are anonymous public pull tokens, never host credentials.
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        need(response.geturl() == url, 'unexpected HTTP destination')
        body = response.read(limit + 1)
    need(len(body) <= limit, 'oversize public response')
    return body


class ReleaseSource:
    """Public GitHub releases and GHCR images for one configured service."""
    def __init__(self, repository, image, signer_workflow):
        need(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository), 'invalid source repository')
        need(re.fullmatch(r'ghcr\.io/[a-z0-9_.-]+/[a-z0-9_./-]+', image) and '..' not in image, 'invalid GHCR image')
        need(signer_workflow.startswith(repository + '/.github/workflows/') and '..' not in signer_workflow and re.fullmatch(r'[A-Za-z0-9_./-]+\.ya?ml', signer_workflow), 'invalid signer workflow')
        self.repository, self.image, self.signer_workflow = repository, image, signer_workflow

    def api(self, path):
        need(path.startswith('/') and not any(c in path for c in ('#', '\\', '\n')), 'invalid API path')
        return json.loads(public_bytes('https://api.github.com/repos/' + self.repository + path,
            {'Accept': 'application/vnd.github+json', 'User-Agent': 'atrinik-service-updater',
             'X-GitHub-Api-Version': '2022-11-28'}))


    def manifest_reference(self, reference):
        return re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[0-9]+\.[0-9]+\.[0-9]+)', reference)

    def manifest(self, reference):
        need(isinstance(reference, str) and self.manifest_reference(reference), 'invalid manifest reference')
        token = json.loads(public_bytes('https://ghcr.io/token?service=ghcr.io&scope=repository:' + self.image[8:] + ':pull', limit=16384)).get('token')
        need(isinstance(token, str) and 0 < len(token) < 16000 and '\n' not in token, 'invalid anonymous registry token')
        raw = public_bytes('https://ghcr.io/v2/' + self.image[8:] + '/manifests/' + reference,
            {'Authorization': 'Bearer ' + token, 'Accept': ', '.join((
                'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
                'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'))})
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        need(not reference.startswith('sha256:') or digest == reference, 'registry manifest digest mismatch')
        return digest, raw


    def validate_pin(self, pin):
        need(type(pin) is dict and pin.get('release_channel', 'stable') == 'stable', 'pin channel mismatch')
        self.validate_image_pin(pin)
        need(type(pin.get('release_id')) is int and pin['release_id'] > 0, 'invalid release identity')
        return pin

    def validate_image_pin(self, pin):
        version(pin['version'])
        need(not pin['version'].startswith('v'), 'pin version must omit tag prefix')
        need(re.fullmatch(re.escape(self.image) + '@sha256:' + HEX, pin['image']), 'invalid image digest')
        need(re.fullmatch(re.escape(self.image) + '@sha256:' + HEX, pin['index_image']), 'invalid image index digest')
        need(re.fullmatch('sha256:' + HEX, pin['config_digest']), 'invalid OCI config digest')
        need(re.fullmatch('sha256:' + HEX, pin['image_id']), 'invalid immutable local image hash')
        need(pin['image_id'] in (pin['config_digest'], pin['image'].split('@', 1)[1], pin['index_image'].split('@', 1)[1]), 'engine image identity is outside attested graph')
        need(re.fullmatch(r'[0-9a-f]{40}', pin['revision']), 'invalid source revision')
        return pin



    def validate_release(self, data):
        need(type(data) is dict and data.get('draft') is False and data.get('prerelease') is False, 'not stable release')
        tag = data['tag_name']
        need(tag.startswith('v'), 'invalid release tag')
        version(tag)
        need(type(data.get('id')) is int and data['id'] > 0, 'invalid release ID')
        need(data.get('html_url') == f'https://github.com/{self.repository}/releases/tag/{tag}', 'wrong release repository')
        published = datetime.datetime.fromisoformat(data['published_at'].replace('Z', '+00:00'))
        need(published.tzinfo is not None and published.timestamp() <= time.time() + 60, 'invalid publication timestamp')
        return tag



    def check_monotonic(self, candidate, ledger):
        self.validate_pin(candidate)
        need(type(ledger) is dict and type(ledger.get('accepted')) is list, 'invalid accepted ledger')
        for accepted in ledger['accepted']:
            self.validate_pin(accepted)
            if candidate['version'] == accepted['version'] or candidate['release_id'] == accepted['release_id']:
                need(candidate == accepted, 'release retag or replacement rejected')
            need(version(candidate['version']) >= version(accepted['version']), 'downgrade rejected')



class DevelopmentSource(ReleaseSource):
    """Opt-in source-ordered OCI publications; no synthetic release identities."""
    def __init__(self, repository, image, signer_workflow, source_ref, discovery_tag):
        super().__init__(repository, image, signer_workflow)
        need(re.fullmatch(r'refs/heads/[A-Za-z0-9_/-]+', source_ref) and '..' not in source_ref,
             'invalid development source ref')
        need(re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', discovery_tag), 'invalid discovery tag')
        self.source_ref, self.discovery_tag = source_ref, discovery_tag

    def manifest_reference(self, reference):
        return (reference == self.discovery_tag or
                re.fullmatch(r'(?:sha256:[0-9a-f]{64}|source-[0-9a-f]{40})', reference))

    def validate_pin(self, pin):
        fields = {'release_channel', 'version', 'revision', 'image', 'index_image',
                  'config_digest', 'image_id'}
        need(type(pin) is dict and set(pin) == fields, 'invalid development pin fields')
        need(pin['release_channel'] == 'development' and pin['version'] == '0.0.0',
             'development pin channel or version mismatch')
        return self.validate_image_pin(pin)

    def check_discovery(self, pin):
        self.validate_pin(pin)
        expected = pin['index_image'].split('@', 1)[1]
        immutable, _ = self.manifest('source-' + pin['revision'])
        alias, _ = self.manifest(self.discovery_tag)
        need(immutable == expected and alias == expected,
             'development source tag or discovery alias changed')

    def check_monotonic(self, candidate, ledger):
        self.validate_pin(candidate)
        need(type(ledger) is dict and type(ledger.get('accepted')) is list, 'invalid accepted ledger')
        for accepted in ledger['accepted']:
            self.validate_pin(accepted)
            if candidate['revision'] == accepted['revision']:
                need(candidate == accepted, 'source revision reused with different image bytes')
            else:
                self.require_ancestor(accepted['revision'], candidate['revision'])

    def require_ancestor(self, ancestor, revision):
        need(re.fullmatch(r'[0-9a-f]{40}', ancestor) and re.fullmatch(r'[0-9a-f]{40}', revision),
             'invalid ancestry revision')
        if ancestor == revision:
            return
        comparison = self.api('/compare/' + ancestor + '...' + revision)
        need(comparison.get('status') == 'ahead' and
             comparison.get('merge_base_commit', {}).get('sha') == ancestor,
             'source rollback or divergence rejected')


def amd64_child(raw):
    index = json.loads(raw)
    need(index.get('schemaVersion') == 2, 'unsupported index schema')
    manifests = [m for m in index.get('manifests', []) if m.get('platform', {}).get('os') == 'linux' and m.get('platform', {}).get('architecture') == 'amd64']
    need(len(manifests) == 1 and re.fullmatch('sha256:' + HEX, manifests[0].get('digest', '')), 'expected unique linux/amd64 manifest')
    return manifests[0]['digest']


def verifier_args(directory, revision, updater_image, repository, workflow, source_ref=None):
    need(re.fullmatch(r'ghcr\.io/atrinik/service-updater@sha256:' + HEX, updater_image), 'updater verifier image must be pinned')
    need(re.fullmatch(r'[0-9a-f]{40}', revision), 'invalid verifier source revision')
    args = ['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only',
        '--user', '65532:65532', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--pids-limit', '64', '--memory', '256m', '--cpus', '1',
        '--tmpfs', '/tmp:size=16m,mode=1777', '--env', 'HOME=/tmp', '--env', 'GH_CONFIG_DIR=/tmp/gh',
        '--mount', 'type=bind,src=' + str(directory) + ',dst=/evidence,readonly',
        '--entrypoint', '/usr/local/bin/gh', updater_image, 'attestation', 'verify', '/evidence/index.json',
        '--bundle', '/evidence/bundles.jsonl', '--custom-trusted-root', '/trusted-root.jsonl',
        '--repo', repository, '--signer-workflow', workflow, '--source-digest', revision, '--format', 'json']
    if source_ref is not None:
        need(re.fullmatch(r'refs/heads/[A-Za-z0-9_/-]+', source_ref), 'invalid verifier source ref')
        args += ['--source-ref', source_ref, '--deny-self-hosted-runners',
                 '--predicate-type', 'https://slsa.dev/provenance/v1']
    return args


def verify_index(raw, revision, attestations, args_builder, runner=command):
    need(isinstance(attestations, list) and 0 < len(attestations) <= 30, 'public attestation unavailable')
    bundles = []
    for entry in attestations:
        need(isinstance(entry, dict) and isinstance(entry.get('bundle'), dict), 'malformed attestation bundle')
        bundles.append(json.dumps(entry['bundle'], separators=(',', ':')))
    # Evidence is public, independently digest-checked OCI metadata only. This
    # directory is the verifier's sole host mount, and is never writable there.
    with tempfile.TemporaryDirectory(prefix='atrinik-public-verification-') as temp:
        directory = Path(temp)
        directory.chmod(0o755)
        (directory / 'index.json').write_bytes(raw)
        (directory / 'bundles.jsonl').write_text('\n'.join(bundles) + '\n')
        for file in directory.iterdir():
            file.chmod(0o444)
        result = json.loads(runner(*args_builder(directory, revision), timeout=300))
        need(isinstance(result, list) and result, 'no verified provenance')


def activate(adapter, candidate, lock=None):
    adapter.fence()
    need(adapter.load(adapter.ROOT / 'policy.json').get('activation_enabled') is True, 'activation policy awaits explicit authorization')
    need(not (adapter.ROOT / 'transaction.json').exists(), 'unfinished transaction requires recovery review')
    current = adapter.load(adapter.ROOT / 'pin.json')
    if candidate == current:
        return
    adapter.check_monotonic(candidate, adapter.load(adapter.ROOT / 'ledger.json'))
    adapter.inspect_image(candidate)
    adapter.verify_provenance(candidate)
    adapter.capability(candidate)
    adapter.compatibility(candidate)
    adapter.headroom()
    need(not adapter.command('docker', 'ps', '-aq', '--filter', 'name=^/' + adapter.CONTAINER + '-candidate$'), 'previous candidate requires review')
    adapter.atomic(adapter.ROOT / 'transaction.json', {'phase': 'draining', 'candidate': candidate})
    # The native admin channel must broadcast and finish a clean save before
    # closing ingress. Unsupported published images leave the live game alone.
    adapter.drain()
    adapter.guard('closed')
    drained = {'phase': 'drained', 'candidate': candidate}
    adapter.atomic(adapter.ROOT / 'transaction.json', drained)
    # ExecStop takes the same lock. During this narrow handoff the durable
    # transaction fences ALL starts and competing updates. Revalidate on return.
    if lock is not None:
        fcntl.flock(lock, fcntl.LOCK_UN)
    try:
        adapter.command('systemctl', 'stop', adapter.SERVICE, timeout=300)
    finally:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
    if lock is not None:
        need(adapter.load(adapter.ROOT / 'transaction.json') == drained, 'transaction changed during stop handoff')
        adapter.fence()
    adapter.stopped()
    backup = adapter.archive()
    adapter.atomic(adapter.ROOT / 'transaction.json', {'backup': str(backup), 'phase': 'testing', 'candidate': candidate})
    adapter.clone_check(backup, candidate)
    adapter.atomic(adapter.ROOT / 'transaction.json', {'backup': str(backup), 'phase': 'closed'})
    try:
        adapter.atomic(adapter.ROOT / 'pin.json', candidate)
        adapter.launch(candidate)
        adapter.healthy(adapter.CONTAINER)
        adapter.require_control(adapter.CONTAINER)
        adapter.validate_access(adapter.identity())
        adapter.validate_runtime(json.loads(adapter.command('docker', 'inspect', adapter.CONTAINER))[0], candidate)
        need(adapter.identity() == adapter.load(backup / 'manifest.json')['identity'], 'live identity changed')
        ledger = adapter.load(adapter.ROOT / 'ledger.json')
        ledger['accepted'].append(candidate)
        adapter.atomic(adapter.ROOT / 'ledger.json', ledger)
        # Durable irreversible boundary BEFORE allowing gameplay. A crash after
        # this write must never restore old player data automatically.
        adapter.atomic(adapter.ROOT / 'transaction.json', {'backup': str(backup), 'phase': 'may-have-played'})
    except Exception:
        # Only this still-isolated candidate can be signalled: public ingress
        # has never opened and no player could have joined.
        if adapter.command('docker', 'ps', '-q', '--filter', 'name=^/' + adapter.CONTAINER + '$'):
            adapter.stop_isolated(adapter.CONTAINER)
        adapter.guard('closed')
        adapter.restore(backup)
        adapter.atomic(adapter.ROOT / 'transaction.json', {'backup': str(backup), 'phase': 'rollback-review'})
        # Restore keeps the service stopped for explicit review of the failure.
        raise
    adapter.guard('public')
    (adapter.ROOT / 'transaction.json').unlink()
    adapter.command('systemctl', '--no-block', 'start', adapter.SERVICE)
    adapter.atomic(backup / 'completed.json', {'backup': backup.name, 'accepted': candidate})
    adapter.retain(backup)


def archive_files(target, backup_root, paths, image_id, runner=command):
    """Archive a stopped complete cohort and image, then fsync before receipt."""
    runner('tar', '--format=pax', '--acls', '--xattrs', '--numeric-owner', '-cpf', target / 'cohort.tar', '-C', '/', *paths, timeout=1800)
    os.chmod(target / 'cohort.tar', 0o600)
    runner('tar', '--compare', '--acls', '--xattrs', '--numeric-owner', '-f', target / 'cohort.tar', '-C', '/', timeout=1800)
    runner('docker', 'image', 'save', '--output', target / 'image.tar', image_id, timeout=1800)
    os.chmod(target / 'image.tar', 0o600)
    # A durable receipt must never precede durable archive contents.
    for archive_file in (target / 'cohort.tar', target / 'image.tar'):
        with archive_file.open('rb') as stream:
            os.fsync(stream.fileno())
    for directory_path in (target, backup_root):
        directory = os.open(directory_path, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
