#!/usr/bin/python3
"""Published development server release controller (MIT). No credentials or subprocess output enter the journal.

Activation remains disabled until a published image supports trusted in-game
countdown control and legacy private-map save support. Never substitute
SIGTERM for an unavailable live-server warning channel.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import socket
import struct
import stat
import subprocess
import sys
import time
import tempfile
import urllib.request
import core

ROOT = Path('/opt/atrinik-development')
STATE = Path('/srv/atrinik/development')
BACKUPS = Path('/var/backups/atrinik-development')
SERVICE = 'atrinik-development.service'
CONTAINER = 'atrinik-development'
IMAGE = 'ghcr.io/atrinik/classic-server'
REPO = 'atrinik/classic'
# No host identity is embedded in the public controller. CLI configuration is mandatory.
PRODUCTION_ID = MACHINE = UUID = DISK_SERIAL = ''
STATE_MOUNT = Path('/srv/atrinik')
PRODUCTION_MARKER = Path('/etc/atrinik-production-migrated')
PRODUCTION_FENCE = Path('/etc/systemd/system/atrinik-runtime.service.d/production-migration-fence.conf')
PRODUCTION_SERVICE = 'atrinik-runtime.service'
PRODUCTION_CONTAINER = 'atrinik-server'
PRODUCTION_ENDPOINT = 'production.invalid'
LEGACY_GUARD = 'atrinik_restore'
UPDATE_SERVICE = 'atrinik-development-update.service'
UPDATE_TIMER = 'atrinik-development-update.timer'
LOCK = Path('/run/lock/atrinik-development-update.lock')
ADMIN = Path('/run/atrinik-development-admin/server.sock')
VERIFIER_IMAGE = ''
CONFIG_PATH = None
INSTALLATION_ROOT = Path('/opt/service-updater')
SIGNER_WORKFLOW = REPO + '/.github/workflows/package-release.yml'
RELEASE_CHANNEL = 'stable'
SOURCE_REF = DISCOVERY_TAG = None
DEVELOPMENT_IMAGE = 'ghcr.io/atrinik/classic-server'
DEVELOPMENT_WORKFLOW = 'atrinik/classic/.github/workflows/publish-development-server.yml'
HEX = core.HEX
Rejected = core.Rejected
need = core.need
command = core.command
atomic = core.atomic
private = core.private
load = core.load
version = core.version
file_hash = core.file_hash
NoRedirect = core.NoRedirect
public_bytes = core.public_bytes

# Every deployment fence is required. There are no skip-fence configuration knobs.
CONFIG_FIELDS = {
    'installation_root': ('INSTALLATION_ROOT', 'path'),
    'root': ('ROOT', 'path'), 'state': ('STATE', 'path'), 'backups': ('BACKUPS', 'path'),
    'state_mount': ('STATE_MOUNT', 'path'), 'admin_socket': ('ADMIN', 'path'),
    'lock': ('LOCK', 'path'), 'production_marker': ('PRODUCTION_MARKER', 'path'),
    'production_fence': ('PRODUCTION_FENCE', 'path'),
    'service': ('SERVICE', 'service'), 'update_service': ('UPDATE_SERVICE', 'service'),
    'update_timer': ('UPDATE_TIMER', 'timer'), 'production_service': ('PRODUCTION_SERVICE', 'service'),
    'container': ('CONTAINER', 'name'), 'production_container': ('PRODUCTION_CONTAINER', 'name'),
    'legacy_guard': ('LEGACY_GUARD', 'name'), 'state_disk_serial': ('DISK_SERIAL', 'name'),
    'machine_id': ('MACHINE', 'machine'), 'state_filesystem_uuid': ('UUID', 'uuid'),
    'production_identity_sha256': ('PRODUCTION_ID', 'digest'),
    'production_endpoint': ('PRODUCTION_ENDPOINT', 'hostname'),
    'updater_image': ('VERIFIER_IMAGE', 'image'),
    'adapter': ('ADAPTER', 'adapter'),
    'release_repository': ('REPO', 'repository'), 'release_image': ('IMAGE', 'release_image'),
    'signer_workflow': ('SIGNER_WORKFLOW', 'workflow'),
}
CONFIG_PATTERNS = {
    'adapter': 'classic', 'repository': 'atrinik/classic',
    'release_image': r'ghcr\.io/atrinik/classic-server',
    'workflow': r'atrinik/classic/\.github/workflows/package-release\.yml',
    'path': r'/[A-Za-z0-9_.\-/]+', 'service': r'[A-Za-z0-9_-]+\.service',
    'timer': r'[A-Za-z0-9_-]+\.timer', 'name': r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',
    'machine': r'[0-9a-f]{32}', 'uuid': r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
    'digest': HEX, 'hostname': r'[a-z0-9]+(?:[.-][a-z0-9]+)+',
    'image': r'ghcr\.io/atrinik/service-updater@sha256:' + HEX,
}


def validate_deployment(data):
    need(type(data) is dict and type(data.get('schema_version')) is int and
         data['schema_version'] in (1, 2), 'unsupported deployment config schema')
    channel = data.get('release_channel', 'stable')
    extra = {'schema_version'}
    if data['schema_version'] == 2:
        extra.add('release_channel')
        need(channel in ('stable', 'development'), 'invalid release channel')
        if channel == 'development':
            extra.update(('source_ref', 'discovery_tag'))
            need(data.get('source_ref') == 'refs/heads/main' and data.get('discovery_tag') == 'development',
                 'invalid development discovery tuple')
    need(set(data) == set(CONFIG_FIELDS) | extra, 'deployment config fields missing or unknown')
    patterns = dict(CONFIG_PATTERNS)
    if channel == 'development':
        patterns.update(release_image=re.escape(DEVELOPMENT_IMAGE), workflow=re.escape(DEVELOPMENT_WORKFLOW))
    values = {'RELEASE_CHANNEL': channel, 'SOURCE_REF': data.get('source_ref'),
              'DISCOVERY_TAG': data.get('discovery_tag')}
    for key, (name, kind) in CONFIG_FIELDS.items():
        value = data[key]
        need(isinstance(value, str) and re.fullmatch(patterns[kind], value), 'invalid deployment config: ' + key)
        if kind == 'path':
            path = Path(value)
            need(str(path) == value and '..' not in path.parts and len(path.parts) >= 3, 'unsafe deployment path: ' + key)
            value = path
        values[name] = value
    need(values['STATE'].is_relative_to(values['STATE_MOUNT']) and values['STATE'] != values['STATE_MOUNT'], 'state must be below pinned mount')
    roots = [values[name] for name in ('ROOT', 'STATE', 'BACKUPS', 'INSTALLATION_ROOT')]
    need(all(not a.is_relative_to(b) for a in roots for b in roots if a != b) and len(set(roots)) == 4, 'deployment roots must be disjoint')
    need(not values['BACKUPS'].is_relative_to(values['STATE_MOUNT']), 'backups cannot use state mount')
    need(len({values['SERVICE'], values['UPDATE_SERVICE'], values['PRODUCTION_SERVICE']}) == 3, 'service identities overlap')
    need(values['CONTAINER'] != values['PRODUCTION_CONTAINER'] and values['CONTAINER'] + '-candidate' != values['PRODUCTION_CONTAINER'], 'container identities overlap')
    need(values['LOCK'].is_relative_to(Path('/run/lock')), 'lock must use host lock directory')
    need(values['ADMIN'].is_relative_to(Path('/run')) and not values['ADMIN'].is_relative_to(Path('/run/lock')), 'admin socket must use dedicated runtime directory')
    return values


def configure(path):
    global CONFIG_PATH
    need(os.geteuid() == 0, 'root required')
    need(path.is_absolute(), 'absolute deployment config path required')
    for parent in path.parents:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022, 'unsafe deployment config parent')
    data = load(path)
    values = validate_deployment(data)
    need(Path(__file__).resolve().parents[1] == values['INSTALLATION_ROOT'], 'controller installation location mismatch')
    globals().update(values)
    CONFIG_PATH = path


def identity(state=None):
    state = STATE if state is None else state
    path = state / 'server-data/quic-identity.pem'
    private(path, 10001, 10001, (0o600, 0o400))
    der = subprocess.run(['openssl', 'x509', '-in', str(path), '-outform', 'DER'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True).stdout
    return hashlib.sha256(der).hexdigest()

def busctl_value(signature, *args):
    """Read one strictly typed result from the system manager's JSON interface."""
    try:
        result = json.loads(command('busctl', '--system', '--json=short', *args))
    except (ValueError, TypeError) as error:
        raise Rejected('invalid systemd D-Bus response') from error
    need(type(result) is dict and set(result) == {'type', 'data'} and result['type'] == signature,
         'unexpected systemd D-Bus signature')
    return result['data']


def loaded_production_fence():
    # GetUnit only resolves an already loaded unit; never load/reload a unit as
    # a side effect of validation. Check its canonical identity before reading
    # conditions, rather than guessing systemd's escaped D-Bus object path.
    service = 'org.freedesktop.systemd1'
    unit_interface = service + '.Unit'
    resolved = busctl_value('o', 'call', service, '/org/freedesktop/systemd1',
                           service + '.Manager', 'GetUnit', 's', PRODUCTION_SERVICE)
    need(type(resolved) is list and len(resolved) == 1 and type(resolved[0]) is str
         and re.fullmatch(r'/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+', resolved[0]),
         'invalid loaded systemd unit path')
    path = resolved[0]
    unit_id = busctl_value('s', 'get-property', service, path, unit_interface, 'Id')
    need(type(unit_id) is str and unit_id == PRODUCTION_SERVICE, 'loaded systemd unit identity mismatch')
    conditions = busctl_value('a(sbbsi)', 'get-property', service, path, unit_interface, 'Conditions')
    need(type(conditions) is list, 'invalid loaded systemd conditions')
    for condition in conditions:
        need(type(condition) is list and len(condition) == 5, 'invalid loaded systemd condition tuple')
        name, trigger, negate, parameter, state = condition
        need(type(name) is str and type(trigger) is bool and type(negate) is bool
             and type(parameter) is str and type(state) is int and -(2 ** 31) <= state < 2 ** 31,
             'invalid loaded systemd condition fields')
    # Non-trigger conditions are mandatory (AND), whereas trigger conditions
    # can be satisfied by an unrelated alternative. State is only the result of
    # the last evaluation: an unevaluated condition (0) is still configured.
    need(any(condition[:4] == ['ConditionPathExists', False, True, str(PRODUCTION_MARKER)]
             for condition in conditions), 'production start fence not loaded')


def fence(require_pin=True):
    need(os.geteuid() == 0, 'root required')
    need(Path('/etc/machine-id').read_text().strip() == MACHINE, 'wrong machine')
    private(PRODUCTION_MARKER, 0, modes=(0o600, 0o644))
    production_fence = PRODUCTION_FENCE
    private(production_fence, 0, modes=(0o600, 0o644))
    need('ConditionPathExists=!' + str(PRODUCTION_MARKER) in production_fence.read_text(), 'production start fence missing')
    loaded_production_fence()
    need(command('systemctl', 'show', PRODUCTION_SERVICE, '--property=ActiveState', '--value') in ('inactive', 'failed'), 'production not stopped')
    need(not command('docker', 'ps', '-q', '--filter', 'name=^/' + re.escape(PRODUCTION_CONTAINER) + '$'), 'production container running')
    mount = json.loads(command('findmnt', '--json', '--mountpoint', STATE_MOUNT, '--output', 'SOURCE,UUID'))['filesystems'][0]
    need(mount['uuid'] == UUID, 'wrong state filesystem')
    need(DISK_SERIAL in command('lsblk', '--raw', '--noheadings', '--inverse', '--output', 'SERIAL', mount['source']).splitlines(), 'wrong state disk')
    for path in (STATE_MOUNT, STATE, STATE / 'config', ROOT, BACKUPS, INSTALLATION_ROOT):
        private(path, 0, modes=(0o700, 0o750), directory=True)
    private(STATE / 'server-data', 10001, 10001, (0o700,), True)
    marker = STATE / 'server-data/.atrinik-initialized'
    need(marker.is_file() and not marker.is_symlink(), 'initialization marker missing')
    private(STATE / 'config/server-custom.cfg', 0, 10001, (0o440, 0o640))
    need(os.stat(STATE).st_dev == os.stat(STATE_MOUNT).st_dev, 'development state is not on pinned filesystem')
    need(all(os.stat(STATE / name).st_dev == os.stat(STATE).st_dev for name in ('server-data', 'config')), 'nested state mount forbidden')
    need(os.stat(BACKUPS).st_dev == os.stat('/').st_dev and os.stat(STATE).st_dev != os.stat(BACKUPS).st_dev, 'backup must use separate OS filesystem')
    expected_identity = load(ROOT / 'identity.json')['sha256']
    need(re.fullmatch(HEX, expected_identity) and expected_identity != PRODUCTION_ID, 'invalid development identity fence')
    need(identity() == expected_identity, 'development identity mismatch')
    validate_config(STATE / 'config/server-custom.cfg')
    legacy_invite = STATE / 'server-data/rendezvous-invite'
    need(not legacy_invite.exists() and not legacy_invite.is_symlink(), 'legacy invitation requires explicit offline migration')
    need(LEGACY_GUARD not in command('nft', 'list', 'tables'), 'legacy guard must be retired explicitly')
    if require_pin:
        current_pin = validate_pin(load(ROOT / 'pin.json'))
        ledger = load(ROOT / 'ledger.json')
        need(type(ledger.get('accepted')) is list and ledger['accepted'] and ledger['accepted'][-1] == current_pin, 'pin and accepted ledger disagree')
        for accepted in ledger['accepted']:
            validate_pin(accepted)
        inspect_image(current_pin)
        validate_access(expected_identity, pin=current_pin)

def native_config(path):
    """Accept an unambiguous subset of native line-oriented CLI configuration.

    Native sections do not scope startup options, long options accept prefixes,
    and config/file-indirection arguments have effects outside this file. Do not
    interpret this format as INI or reproduce those unsafe extension mechanisms.
    """
    controlled = {'access_required', 'access_initialize', 'access_store',
                  'access_admin_accounts', 'join_password', 'join_password_file',
                  'rendezvous_invite_file', 'metaserver_hostname', 'server_desc', 'server_public'}
    raw = path.read_bytes()
    need(len(raw) <= 1024 * 1024, 'oversize native configuration')
    values = {}
    section = None
    sections = set()
    lines = raw.split(b'\n')
    for number, physical in enumerate(lines):
        # The native fgets buffer is 4096 bytes. A longer comment can itself
        # split into an active directive, so bound lines before ignoring them.
        need(len(physical) <= 4094, 'oversize native configuration line')
        if physical.endswith(b'\r') and number < len(lines) - 1:
            physical = physical[:-1]
        try:
            line = physical.decode('utf-8')
        except UnicodeError:
            raise Rejected('invalid native configuration encoding') from None
        need(not any((ord(char) < 32 and char != '\t') or ord(char) == 127 for char in line) and
             '\ufeff' not in line and '\\' not in line and '<' not in line,
             'native configuration controls or indirection forbidden')
        if not line.strip(' \t'):
            continue
        need(not line.startswith((' ', '\t')), 'indented native configuration forbidden')
        if line.startswith('#'):
            continue
        if re.fullmatch(r'\[[a-z][a-z0-9_]*\]', line):
            section = line[1:-1]
            need(section not in sections, 'duplicate native configuration section')
            sections.add(section)
            continue
        name, separator, value = line.partition('=')
        name, value = name.strip(' \t'), value.strip(' \t')
        need(section is not None and separator == '=' and re.fullmatch(r'[a-z][a-z0-9_]*', name) and
             value != '', 'unsupported native configuration assignment')
        # snprintf adds "--" to key=value inside another 4096-byte buffer.
        need(len((name + '=' + value).encode('utf-8')) <= 4093,
             'native configuration assignment would truncate')
        need(name not in values, 'duplicate native configuration option')
        need(name != 'config' and not any(option.startswith(name) and option != name
             for option in controlled | {'config'}), 'native configuration include or alias forbidden')
        need(name != 'access_admin_accounts',
             'obsolete access administrator configuration requires explicit offline migration')
        if name in controlled:
            need(section == 'meta' and '"' not in value and "'" not in value,
                 'controlled native option requires unquoted meta assignment')
        values[name] = value
    return values


def native_visibility(values):
    # Visibility is independent of access policy and publication channel. Keep
    # the historical public default only when no explicit native option exists.
    public = values.get('server_public', 'true')
    need(public in ('true', 'false'), 'server_public must be true or false')
    return public


def configured_visibility(state):
    return native_visibility(native_config(state / 'config/server-custom.cfg'))


def validate_config(path):
    values = native_config(path)
    native_visibility(values)
    need(not any(name in values for name in ('join_password', 'join_password_file', 'rendezvous_invite_file')),
         'legacy access configuration requires explicit offline migration')
    required = values.get('access_required')
    need(required in ('true', 'false'), 'explicit access_required=true|false policy required')
    need(values.get('access_initialize', 'false') == 'false', 'updater cannot initialize access state')
    need(values.get('access_store') in (None, '/opt/atrinik/server/data/access-tokens'), 'external access store forbidden')
    need('metaserver_hostname' not in values, 'direct endpoint publication forbidden')
    need(PRODUCTION_ENDPOINT not in values.get('server_desc', ''), 'production direct address forbidden')
    return {'policy': 'protected' if required == 'true' else 'open'}


def access_paths(state):
    policy = validate_config(state / 'config/server-custom.cfg')
    obsolete = state / 'config/access-admin-accounts'
    need(not obsolete.exists() and not obsolete.is_symlink(),
         'obsolete access administrator file requires explicit offline migration')
    store = state / 'server-data/access-tokens'
    need(not store.is_symlink(), 'access store symlink forbidden')
    if store.exists():
        private(store, 10001, 10001, (0o700,), True)
        need({path.name for path in store.iterdir()} == {'access-tokens.snapshot'}, 'unexpected access store contents')
        private(store / 'access-tokens.snapshot', 10001, 10001, (0o600,))
    else:
        need(policy['policy'] == 'open', 'protected access store missing')
    return policy


def access_footprint(state):
    """Bind complete authorization state, without recording credential contents."""
    access_paths(state)
    private(state / 'config/server-custom.cfg', 0, 10001, (0o440, 0o640))
    private(state / 'server-data/quic-identity.pem', 10001, 10001, (0o600, 0o400))
    paths = ['config/server-custom.cfg', 'server-data/quic-identity.pem']
    snapshot = 'server-data/access-tokens/access-tokens.snapshot'
    if (state / snapshot).exists():
        paths.append(snapshot)
    return {name: file_hash(state / name) for name in paths}


def offline_access_status(state, pin, expected_identity, policy):
    # The native inspector independently takes the existing data-directory lock,
    # reads the full store without initialization/reconciliation, and never starts
    # a server. Read-only mounts enforce that boundary even on an image regression.
    inspect_image(pin)
    verify_provenance(pin)
    args = ['docker', 'run', '--rm', '--pull', 'never', '--platform', 'linux/amd64',
            '--network', 'none', '--read-only', '--user', '10001:10001',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--pids-limit', '64', '--memory', '128m', '--cpus', '1',
            '--mount', 'type=bind,src=' + str(state / 'server-data') + ',dst=/opt/atrinik/server/data,readonly',
            '--entrypoint', '/opt/atrinik/server/atrinik-access-status', pin['image_id'],
            '--data-dir', '/opt/atrinik/server/data',
            '--store-dir', '/opt/atrinik/server/data/access-tokens',
            '--certificate', '/opt/atrinik/server/data/quic-identity.pem', '--policy', policy]
    return validate_access_status(access_json(command(*args, timeout=30)), expected_identity, policy)


def validate_access(expected_identity, state=None, pin=None, name=None, admin=None):
    state = STATE if state is None else state
    pin = load(ROOT / 'pin.json') if pin is None else pin
    name = CONTAINER if name is None else name
    policy = access_paths(state)['policy']
    if command('docker', 'ps', '-q', '--filter', 'name=^/' + re.escape(name) + '$'):
        obj = json.loads(command('docker', 'inspect', name))[0]
        if state == STATE:
            validate_runtime(obj, pin)
        capabilities = admin_capabilities(admin_request('ATRINIK-ADMIN/1 CAPABILITIES', obj['State']['Pid'], admin))
        need('access-tokens-v1' in capabilities, 'native access-token status capability missing')
        return admin_access_status(obj['State']['Pid'], expected_identity, policy, admin)
    return offline_access_status(state, pin, expected_identity, policy)


def validate_pin(pin):
    return source().validate_pin(pin)
def validate_release(data):
    return source().validate_release(data)
def check_monotonic(candidate, ledger):
    return source().check_monotonic(candidate, ledger)







def source():
    if RELEASE_CHANNEL == 'development':
        return core.DevelopmentSource(REPO, IMAGE, SIGNER_WORKFLOW, SOURCE_REF, DISCOVERY_TAG)
    return core.ReleaseSource(REPO, IMAGE, SIGNER_WORKFLOW)


def api(path):
    return source().api(path)


def manifest(reference):
    return source().manifest(reference)


amd64_child = core.amd64_child


def verifier_args(directory, revision, workflow=None):
    workflow = SIGNER_WORKFLOW if workflow is None else workflow
    return core.verifier_args(directory, revision, VERIFIER_IMAGE, REPO, workflow,
                              SOURCE_REF if RELEASE_CHANNEL == 'development' else None)


def verify_index(raw, revision, workflow=None):
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    attestations = api('/attestations/' + digest + '?per_page=30').get('attestations')
    return core.verify_index(raw, revision, attestations,
        lambda directory, rev: verifier_args(directory, rev, workflow), command)


class ClassicAdapter:
    """Resolve current globals so service hooks retain their existing tests."""
    hooks = {'drain': 'admin_stop', 'capability': 'image_capability',
             'compatibility': 'accepted_compatibility', 'require_control': 'require_admin',
             'stop_isolated': 'graceful_stop'}

    def __getattr__(self, name):
        return globals()[self.hooks.get(name, name)]


def activate(candidate, lock=None):
    return core.activate(ClassicAdapter(), candidate, lock)


def stage():
    if RELEASE_CHANNEL == 'development':
        return stage_development()
    release = api('/releases/latest')
    tag = validate_release(release)
    commit = api('/git/ref/tags/' + tag)['object']
    for _ in range(4):
        need(re.fullmatch(r'[0-9a-f]{40}', commit['sha']), 'invalid tag object')
        if commit['type'] == 'commit':
            break
        need(commit['type'] == 'tag', 'unexpected tag type')
        commit = api('/git/tags/' + commit['sha'])['object']
    need(commit['type'] == 'commit', 'tag nesting too deep')
    index_digest, raw = manifest(tag[1:])
    index_image = IMAGE + '@' + index_digest
    digest = amd64_child(raw)
    _, child = manifest(digest)
    config_digest = json.loads(child).get('config', {}).get('digest')
    need(isinstance(config_digest, str) and re.fullmatch('sha256:' + HEX, config_digest), 'invalid child config digest')
    candidate = {'version': tag[1:], 'image': IMAGE + '@' + digest, 'index_image': index_image,
                 'revision': commit['sha'], 'release_id': release['id'], 'config_digest': config_digest}
    command('docker', 'pull', '--platform', 'linux/amd64', candidate['image'], timeout=900)
    pulled = json.loads(command('docker', 'image', 'inspect', candidate['image']))[0]
    need(candidate['image'] in pulled.get('RepoDigests', []), 'pulled digest mismatch')
    candidate['image_id'] = pulled['Id']
    check_monotonic(candidate, load(ROOT / 'ledger.json'))
    inspect_image(candidate)
    # Verify GitHub's signed build attestation, not just mutable OCI labels.
    verify_provenance(candidate)
    image_capability(candidate)
    accepted_compatibility(candidate)
    atomic(ROOT / 'staged.json', candidate)
    return candidate

def stage_development():
    publication = source()
    index_digest, raw = publication.manifest(DISCOVERY_TAG)
    child_digest = amd64_child(raw)
    _, child = publication.manifest(child_digest)
    config_digest = json.loads(child).get('config', {}).get('digest')
    need(isinstance(config_digest, str) and re.fullmatch('sha256:' + HEX, config_digest),
         'invalid child config digest')
    image = IMAGE + '@' + child_digest
    command('docker', 'pull', '--platform', 'linux/amd64', image, timeout=900)
    pulled = json.loads(command('docker', 'image', 'inspect', image))[0]
    need(image in pulled.get('RepoDigests', []), 'pulled digest mismatch')
    candidate = {'release_channel': 'development', 'version': '0.0.0',
                 'revision': (pulled['Config'].get('Labels') or {}).get('org.opencontainers.image.revision', ''),
                 'image': image, 'index_image': IMAGE + '@' + index_digest,
                 'config_digest': config_digest, 'image_id': pulled['Id']}
    check_monotonic(candidate, load(ROOT / 'ledger.json'))
    inspect_image(candidate)
    verify_provenance(candidate)
    publication.check_discovery(candidate)
    accepted_compatibility(candidate)
    # Never execute candidate bytes before provenance, ordering and discovery
    # checks have all completed. Initial acceptance is operator-owned.
    image_capability(candidate)
    atomic(ROOT / 'staged.json', candidate)
    return candidate


def verify_provenance(pin):
    validate_pin(pin)
    _, raw = manifest(pin['index_image'].split('@', 1)[1])
    need(IMAGE + '@' + amd64_child(raw) == pin['image'], 'attested index child mismatch')
    verify_index(raw, pin['revision'])
    _, child = manifest(pin['image'].split('@', 1)[1])
    need(json.loads(child).get('config', {}).get('digest') == pin['config_digest'], 'attested child and config digest disagree')


def accepted_compatibility(pin):
    acceptance = load(ROOT / 'acceptance.json')
    need(acceptance.get('private_map_roundtrip') is True and type(acceptance.get('private_map_count')) is int and acceptance['private_map_count'] >= 5, 'actual private-map roundtrip acceptance required')
    if RELEASE_CHANNEL == 'stable':
        need(version(pin['version']) >= version(acceptance['minimum_version']), 'release predates accepted private-map fix')
    revision = acceptance['source_revision']
    need(re.fullmatch(r'[0-9a-f]{40}', revision), 'invalid accepted source revision')
    if revision != pin['revision']:
        comparison = api('/compare/' + revision + '...' + pin['revision'])
        need(comparison.get('status') == 'ahead' and comparison.get('merge_base_commit', {}).get('sha') == revision, 'release does not include accepted compatibility fix')


def image_capability(pin):
    help_text = command('docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none', '--user', '10001:10001',
                        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                        '--entrypoint', '/opt/atrinik/server/atrinik-server', pin['image_id'], '--help')
    need('admin_shutdown_socket' in help_text and 'access_required' in help_text,
         'published image lacks native countdown or access-token capability')


def inspect_image(pin):
    validate_pin(pin)
    obj = json.loads(command('docker', 'image', 'inspect', pin['image_id']))[0]
    need(obj['Architecture'] == 'amd64' and obj['Os'] == 'linux', 'wrong image platform')
    need(obj['Id'] == pin['image_id'], 'image content hash mismatch')
    labels = obj['Config'].get('Labels') or {}
    if RELEASE_CHANNEL == 'development':
        need(labels.get('org.atrinik.release-channel') == 'development', 'OCI channel mismatch')
    for name, value in {'revision': pin['revision'], 'version': pin['version'], 'source': 'https://github.com/' + REPO}.items():
        need(labels.get('org.opencontainers.image.' + name) == value, 'OCI release provenance mismatch')

def guard(mode):
    need(mode in ('closed', 'public'), 'invalid guard mode')
    command('nft', '--file', ROOT / (mode + '.nft'))

def container_args(name, state, pin, isolated, admin_root=None):
    public = 'false' if isolated else configured_visibility(state)
    args = ['docker', 'run', '--platform', 'linux/amd64', '--name', name, '--network', 'none' if isolated else 'host',
            '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--tmpfs', '/tmp:size=32m,mode=1777', '--stop-timeout', '60',
            '--mount', f'type=bind,src={state}/server-data,dst=/opt/atrinik/server/data',
            '--mount', f'type=bind,src={state}/config/server-custom.cfg,dst=/opt/atrinik/server/server-custom.cfg,readonly',
            '--env', 'HOME=/tmp', '--env', 'ATRINIK_HTTP_URL=off', '--env', 'ATRINIK_PORT_MAPPING=off',
            '--env', 'ATRINIK_NETWORK_STACK=ipv4', '--env', 'ATRINIK_QUIC_PORT=1730',
            '--env', 'ATRINIK_SERVER_PUBLIC=' + public,
            '--health-cmd', '/usr/local/bin/atrinik-server-healthcheck', '--health-interval', '10s',
            '--health-timeout', '5s', '--health-retries', '3', '--health-start-period', '20s']
    if not isolated or admin_root:
        args += ['--mount', f'type=bind,src={admin_root or ADMIN.parent},dst={ADMIN.parent}',
                 '--env', 'ATRINIK_ADMIN_SHUTDOWN_SOCKET=' + str(ADMIN)]
    return args + ['--label', 'org.atrinik.development.managed=true', '--detach', pin['image_id']]

def healthy(name):
    for _ in range(36):
        obj = json.loads(command('docker', 'inspect', name))[0]
        need(obj['State']['Running'] and not obj['State'].get('OOMKilled'), 'candidate exited or OOM')
        if obj['State'].get('Health', {}).get('Status') == 'healthy':
            return
        time.sleep(5)
    raise Rejected('health timeout')

@contextlib.contextmanager
def admin_connection(request, expected_pid, admin=None):
    admin = ADMIN if admin is None else admin
    private(admin.parent, 10001, 10001, (0o700,), True)
    info = admin.lstat()
    need(stat.S_ISSOCK(info.st_mode) and info.st_uid == 10001 and stat.S_IMODE(info.st_mode) == 0o600, 'unsafe admin socket')
    encoded = request.encode('ascii') + b'\n'
    need(len(encoded) <= 1024 and encoded.count(b'\n') == 1, 'invalid admin request')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(admin))
        pid, uid, gid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        need(pid == expected_pid and uid == gid == 10001, 'wrong admin peer identity')
        client.sendall(encoded)
        client.shutdown(socket.SHUT_WR)
        yield client


def admin_request(request, expected_pid, admin=None):
    with admin_connection(request, expected_pid, admin) as client:
        # Read through EOF so trailing frames cannot hide behind the first LF.
        limit = 2048 if request == 'ATRINIK-ADMIN/1 CAPABILITIES' else 1024
        response = b''
        while len(response) <= limit:
            chunk = client.recv(limit + 1 - len(response))
            if not chunk:
                break
            response += chunk
    need(len(response) <= limit and response.endswith(b'\n') and response.count(b'\n') == 1 and response.isascii(), 'malformed admin response')
    return response[:-1].decode('ascii')


def access_json(raw):
    """No duplicate keys, non-JSON numbers or extra output from native status."""
    need(isinstance(raw, (str, bytes)) and len(raw) <= 32768, 'invalid access status size')
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'duplicate access status field')
            result[key] = value
        return result
    def invalid_constant(value):
        raise Rejected('non-JSON access status number')
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError):
        raise Rejected('malformed access status JSON') from None
    need(type(result) is dict, 'invalid access status object')
    return result


def access_revision(value):
    need(type(value) is str and re.fullmatch(r'0|[1-9][0-9]{0,19}', value) and
         int(value) <= 2**64 - 1, 'invalid access store revision')
    return value


def validate_access_status(status, expected_identity, policy):
    need(type(status) is dict and policy in ('open', 'protected'), 'invalid access status')
    base = {'state', 'schemaVersion', 'serverIdentity', 'policy'}
    need(type(status.get('schemaVersion')) is int and status['schemaVersion'] == 1 and
         status.get('serverIdentity') == expected_identity and re.fullmatch(HEX, expected_identity) and
         status.get('policy') == policy, 'access status identity, policy or schema mismatch')
    if status.get('state') == 'absent_open':
        need(policy == 'open' and set(status) == base, 'absent protected or malformed access store')
    else:
        need(status.get('state') == 'initialized' and
             set(status) == base | {'integrity', 'durability', 'revision', 'pendingRouteSync'},
             'unsupported access store status')
        need(status['integrity'] == 'ok' and status['durability'] == 'ok', 'access store is not durably valid')
        access_revision(status['revision'])
        need(type(status['pendingRouteSync']) is int and 0 <= status['pendingRouteSync'] <= 1024,
             'invalid pending access route count')
    return status


def admin_access_status(expected_pid, expected_identity, policy, admin=None):
    request_id = secrets.token_hex(16)
    request = {'schema': 'atrinik-access-admin-v1', 'operation': 'status', 'requestId': request_id}
    with admin_connection('ATRINIK-ADMIN/1 ACCESS ' + json.dumps(request, separators=(',', ':')),
                          expected_pid, admin) as client:
        header = b''
        while len(header) < 30 and not header.endswith(b'\n'):
            chunk = client.recv(1)
            need(bool(chunk), 'truncated access status header')
            header += chunk
        match = re.fullmatch(rb'ATRINIK-ADMIN/1 ACCESS ([1-9][0-9]{0,4})\n', header)
        need(match is not None and int(match[1]) <= 32768, 'invalid access status framing')
        length = int(match[1])
        payload = b''
        while len(payload) < length:
            chunk = client.recv(length - len(payload))
            need(bool(chunk), 'truncated access status body')
            payload += chunk
        need(client.recv(1) == b'', 'trailing access status bytes')
    response = access_json(payload)
    need(set(response) == {'schema', 'operation', 'requestId', 'outcome', 'revision', 'result'} and
         response['schema'] == request['schema'] and response['operation'] == 'status' and
         response['requestId'] == request_id and response['outcome'] == 'committed',
         'access status response binding or outcome mismatch')
    status = validate_access_status(response['result'], expected_identity, policy)
    need(response['revision'] == status.get('revision'), 'access status revision mismatch')
    return status


def admin_capabilities(response):
    """Parse the bounded native capability set; unknown tokens are extensible."""
    prefix = 'ATRINIK-ADMIN/1 CAPABILITIES '
    need(isinstance(response, str) and response.isascii() and
         len(response) + 1 <= 2048 and response.startswith(prefix),
         'malformed admin capabilities')
    tokens = response[len(prefix):].split(' ')
    need(1 <= len(tokens) <= 32 and
         all(re.fullmatch(r'[a-z0-9-]{1,48}', token) for token in tokens) and
         len(set(tokens)) == len(tokens), 'malformed admin capabilities')
    capabilities = frozenset(tokens)
    need({'shutdown-v1', 'durable-result-v1'} <= capabilities,
         'native countdown capabilities missing')
    return capabilities


def require_admin(name):
    obj = json.loads(command('docker', 'inspect', name))[0]
    admin_capabilities(admin_request('ATRINIK-ADMIN/1 CAPABILITIES', obj['State']['Pid']))

def admin_stop(name=None, admin=None):
    name = CONTAINER if name is None else name
    admin = ADMIN if admin is None else admin
    if not command('docker', 'ps', '-q', '--filter', 'name=^/' + name + '$'):
        return
    obj = json.loads(command('docker', 'inspect', name))[0]
    if name == CONTAINER:
        # A stale visibility override must not prevent the authenticated
        # countdown that stops it. All runtime identity fences still apply.
        validate_runtime(obj, load(ROOT / 'pin.json'), check_visibility=False)
    expected_id = obj['Id']
    pid = obj['State']['Pid']
    admin_capabilities(admin_request('ATRINIK-ADMIN/1 CAPABILITIES', pid, admin))
    request_id = secrets.token_hex(16)
    result = admin.with_name(admin.name + '.' + request_id + '.result')
    need(not result.exists(), 'admin request result already exists')
    request = 'ATRINIK-ADMIN/1 SHUTDOWN ' + request_id + ' 60 Automatic development update; reconnect after restart.'
    need(admin_request(request, pid, admin) == 'ATRINIK-ADMIN/1 SCHEDULED ' + request_id, 'countdown not acknowledged')
    for _ in range(80):
        obj = json.loads(command('docker', 'inspect', name))[0]
        need(obj['Id'] == expected_id, 'container changed during countdown')
        if not obj['State']['Running']:
            need(obj['State']['ExitCode'] == 0 and not obj['State'].get('OOMKilled'), 'unclean scheduled shutdown')
            private(result, 10001, 10001, (0o600,))
            need(result.read_text() == 'ATRINIK-ADMIN/1 RESULT ' + request_id + ' saved\n', 'native save result failed or unavailable')
            return
        time.sleep(3)
    raise Rejected('countdown timeout; server left alone, no signal fallback')


def graceful_stop(name):
    objects = json.loads(command('docker', 'inspect', name))
    if objects[0]['State']['Running']:
        command('docker', 'kill', '--signal', 'TERM', name)
    for _ in range(30):
        obj = json.loads(command('docker', 'inspect', name))[0]
        if not obj['State']['Running']:
            need(obj['State']['ExitCode'] == 0 and not obj['State'].get('OOMKilled'), 'unclean server exit')
            result = subprocess.run(['docker', 'logs', '--tail', '2000', name], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
            need(result.returncode == 0, 'cannot inspect shutdown logs')
            logs = result.stdout
            need('Server shutdown complete' in logs, 'missing graceful shutdown marker')
            need(not re.search(r'(?:BUG|ERROR).*?(?:sav|persist|journal)|(?:sav|persist|journal).*?(?:fail|error)', logs, re.IGNORECASE), 'possible persistence failure requires review')
            return
        time.sleep(3)
    raise Rejected('graceful shutdown timeout; container retained, no forced kill')

def stopped():
    status = command('systemctl', 'show', SERVICE, '--property=ActiveState', '--value')
    need(status in ('inactive', 'failed'), 'activation deferred: use in-game /shutdown countdown')
    need(command('docker', 'ps', '--filter', 'name=^/' + CONTAINER + '$', '--format', '{{.ID}}') == '', 'container still running')

def headroom():
    size = int(command('du', '--summarize', '--block-size=1', STATE).split()[0])
    image_size = json.loads(command('docker', 'image', 'inspect', load(ROOT / 'pin.json')['image_id']))[0]['Size']
    need(shutil.disk_usage(BACKUPS).free > size * 3 + image_size + 1024 ** 3, 'insufficient backup and clone capacity')
    need(shutil.disk_usage(STATE).free > size + 1024 ** 3, 'insufficient rollback capacity on state filesystem')

def archive():
    stopped()
    headroom()
    target = BACKUPS / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    target.mkdir(mode=0o700)
    paths = [str(STATE).lstrip('/'), str(ROOT).lstrip('/'), str(INSTALLATION_ROOT).lstrip('/'),
             'etc/systemd/system/' + SERVICE,
             'etc/systemd/system/' + UPDATE_SERVICE,
             'etc/systemd/system/' + UPDATE_TIMER]
    if CONFIG_PATH is not None and not CONFIG_PATH.is_relative_to(ROOT):
        paths.append(str(CONFIG_PATH).lstrip('/'))
    authorization = access_footprint(STATE)
    core.archive_files(target, BACKUPS, paths, load(ROOT / 'pin.json')['image_id'], command)
    need(access_footprint(STATE) == authorization, 'authorization changed during backup')
    atomic(target / 'manifest.json', {'sha256': file_hash(target / 'cohort.tar'), 'image_sha256': file_hash(target / 'image.tar'), 'pin': load(ROOT / 'pin.json'), 'identity': identity(), 'access_state': authorization})
    return target

def protected_saves(state):
    result = {}
    for name in ('accounts', 'players', 'unique-items'):
        root = state / 'server-data' / name
        need(root.is_dir() and not root.is_symlink(), 'protected save root missing')
        for path in root.rglob('*'):
            info = path.lstat()
            need(not stat.S_ISLNK(info.st_mode), 'symlink in protected saves')
            if stat.S_ISREG(info.st_mode):
                result[str(path.relative_to(state))] = file_hash(path)
            else:
                need(stat.S_ISDIR(info.st_mode), 'unsupported protected save type')
    return result


def clone_check(backup, candidate):
    clone = backup / 'clone'
    clone.mkdir(mode=0o700)
    command('tar', '--acls', '--xattrs', '--numeric-owner', '-xpf', backup / 'cohort.tar', '-C', clone, str(STATE).lstrip('/'), timeout=1800)
    copy = clone / str(STATE).lstrip('/')
    saves_before = protected_saves(copy)
    authorization = access_footprint(copy)
    need(authorization == load(backup / 'manifest.json')['access_state'], 'cloned authorization differs from backup')
    offline_access_status(copy, candidate, identity(copy), access_paths(copy)['policy'])
    admin_root = clone / 'admin'
    admin_root.mkdir(mode=0o700)
    os.chown(admin_root, 10001, 10001)
    name = CONTAINER + '-candidate'
    need(not command('docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'), 'candidate name already exists')
    try:
        command(*container_args(name, copy, candidate, True, admin_root))
        healthy(name)
        validate_access(identity(copy), copy, candidate, name, admin_root / ADMIN.name)
        admin_stop(name, admin_root / ADMIN.name)
        obj = json.loads(command('docker', 'inspect', name))[0]
        need(obj['State']['ExitCode'] == 0, 'candidate unclean shutdown')
        need(identity(copy) == load(backup / 'manifest.json')['identity'], 'candidate identity changed')
        need(access_footprint(copy) == authorization, 'isolated candidate changed access grants, audit or route state')
        need(protected_saves(copy) == saves_before, 'isolated candidate changed protected saves or private maps')
        # No map may disappear into quarantine. Current normal startup retains
        # legacy private maps; any future incompatible activation fails closed.
        need(not any((copy / 'server-data').rglob('*quarantine*')), 'private maps must remain playable; quarantine rejected')
    finally:
        # A timed-out candidate is retained for inspection; never SIGKILL a saver.
        command('docker', 'rm', name, allowed=(0, 1))
    shutil.rmtree(clone)

def restore(backup):
    stopped()
    manifest = load(backup / 'manifest.json')
    need(file_hash(backup / 'cohort.tar') == manifest['sha256'], 'rollback archive checksum mismatch')
    need(file_hash(backup / 'image.tar') == manifest['image_sha256'], 'rollback image archive checksum mismatch')
    # A local operator or candidate may have committed a newer revocation while
    # ingress was closed. Never replace it with an older authorization snapshot.
    need(access_footprint(STATE) == manifest.get('access_state'),
         'authorization changed after backup; explicit coherent recovery required')
    command('docker', 'image', 'load', '--input', backup / 'image.tar', timeout=1800)
    inspect_image(manifest['pin'])
    failed = STATE.with_name(STATE.name + '-failed-' + backup.name)
    need(not failed.exists(), 'failed cohort destination exists')
    STATE.rename(failed)
    command('tar', '--acls', '--xattrs', '--numeric-owner', '-xpf', backup / 'cohort.tar', '-C', '/',
            str(STATE).lstrip('/'), str(ROOT).lstrip('/'), timeout=1800)
    need(identity() == manifest['identity'], 'rollback identity mismatch')
    need(load(ROOT / 'pin.json') == manifest['pin'], 'rollback pin mismatch')

def retain(backup):
    archives = sorted(p for p in BACKUPS.iterdir() if p.is_dir() and re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', p.name))
    completed = []
    for old in archives:
        # Failed/incomplete archives and retained clones are a separate set;
        # they never consume or displace a verified completed retention slot.
        if (old / 'clone').exists() or not (old / 'completed.json').exists():
            continue
        private(old, 0, modes=(0o700,), directory=True)
        receipt = load(old / 'completed.json')
        need(receipt['backup'] == old.name, 'retention receipt mismatch')
        manifest = load(old / 'manifest.json')
        need(file_hash(old / 'cohort.tar') == manifest['sha256'], 'retention archive checksum mismatch')
        need(file_hash(old / 'image.tar') == manifest['image_sha256'], 'retention image checksum mismatch')
        completed.append(old)
    keep = set(completed[:1] + completed[-6:] + [backup])
    for old in completed:
        if old not in keep:
            shutil.rmtree(old)

def validate_runtime(obj, pin, *, check_visibility=True):
    need(obj.get('Platform') == 'linux', 'runtime platform mismatch')
    descriptor = obj.get('ImageManifestDescriptor')
    if descriptor is not None:
        need(descriptor.get('digest') == pin['image'].split('@', 1)[1] and descriptor.get('platform', {}).get('os') == 'linux' and descriptor.get('platform', {}).get('architecture') == 'amd64', 'runtime child manifest mismatch')
    else:
        need(pin['image_id'] != pin['index_image'].split('@', 1)[1], 'index-backed runtime requires selected child descriptor')
    need(obj.get('Image') == pin['image_id'] and obj['Config']['Image'] == pin['image_id'] and obj['Config'].get('Labels', {}).get('org.atrinik.development.managed') == 'true', 'unexpected existing runtime')
    need(obj['Config']['User'] == '10001:10001' and obj['HostConfig']['NetworkMode'] == 'host', 'runtime security mismatch')
    mounts = {m['Destination']: (m['Source'], m['RW']) for m in obj['Mounts'] if m['Type'] == 'bind'}
    expected_mounts = {'/opt/atrinik/server/data': (str(STATE / 'server-data'), True),
                       '/opt/atrinik/server/server-custom.cfg': (str(STATE / 'config/server-custom.cfg'), False),
                       str(ADMIN.parent): (str(ADMIN.parent), True)}
    need(mounts == expected_mounts, 'runtime cohort mount mismatch')
    environment = obj['Config'].get('Env')
    need(type(environment) is list and all(type(value) is str for value in environment),
         'invalid runtime environment')
    visibility = [value for value in environment if value.split('=', 1)[0] == 'ATRINIK_SERVER_PUBLIC']
    if check_visibility:
        need(visibility == ['ATRINIK_SERVER_PUBLIC=' + configured_visibility(STATE)],
             'runtime visibility differs from native configuration')


def launch(pin):
    private(ADMIN.parent, 10001, 10001, (0o700,), True)
    need(not command('docker', 'ps', '-q', '--filter', 'name=^/' + CONTAINER + '$'), 'runtime already exists')
    command('docker', 'rm', CONTAINER, allowed=(0, 1))
    command(*container_args(CONTAINER, STATE, pin, False))


def run():
    # Hold through every startup side effect. Updater launches and validates the
    # container itself under the same lock, then queues this supervisor without
    # waiting; no generic transaction bypass or interleaved backup is possible.
    with open(LOCK, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fence()
        need(not (ROOT / 'transaction.json').exists(), 'unfinished activation requires review')
        pin = load(ROOT / 'pin.json')
        verify_provenance(pin)
        image_capability(pin)
        accepted_compatibility(pin)
        guard('closed')
        if command('docker', 'ps', '-q', '--filter', 'name=^/' + CONTAINER + '$'):
            obj = json.loads(command('docker', 'inspect', CONTAINER))[0]
            validate_runtime(obj, pin)
        else:
            launch(pin)
        healthy(CONTAINER)
        require_admin(CONTAINER)
        validate_access(identity())
        fence()
        guard('public')
    return int(command('docker', 'wait', CONTAINER, timeout=365 * 86400))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=('check', 'run', 'stop', 'update', 'stage', 'close'))
    args = parser.parse_args()
    os.umask(0o077)
    configure(args.config)
    if args.action == 'check':
        fence()
        return 0
    if args.action == 'run':
        return run()
    if args.action == 'stop':
        with open(LOCK, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            admin_stop()
        return 0
    with open(LOCK, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fence()
        if args.action == 'check':
            return 0
        if args.action == 'close':
            guard('closed')
            return 0
        candidate = stage()
        if args.action == 'update':
            activate(candidate, lock)
    return 0
