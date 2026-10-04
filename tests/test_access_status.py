"""Access status framing, policy, preservation and authorization rollback gates."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adapters import classic as d

IDENTITY = 'a' * 64
REQUEST_ID = 'b' * 32


def status(policy='protected', **changes):
    value = {'state': 'initialized', 'schemaVersion': 1, 'serverIdentity': IDENTITY,
             'policy': policy, 'integrity': 'ok', 'durability': 'ok',
             'revision': '0', 'pendingRouteSync': 0}
    value.update(changes)
    return value


def absent():
    return {'state': 'absent_open', 'schemaVersion': 1, 'serverIdentity': IDENTITY, 'policy': 'open'}


def envelope(value):
    return {'schema': 'atrinik-access-admin-v1', 'operation': 'status', 'requestId': REQUEST_ID,
            'outcome': 'committed', 'revision': value.get('revision'), 'result': value}


def frame(value):
    raw = json.dumps(value, separators=(',', ':')).encode()
    return b'ATRINIK-ADMIN/1 ACCESS ' + str(len(raw)).encode() + b'\n' + raw


def fixture(root, policy='protected'):
    state = root / 'state'
    (state / 'config').mkdir(parents=True)
    (state / 'config/server-custom.cfg').write_text('[meta]\naccess_required=' + ('true' if policy == 'protected' else 'false') + '\n')
    (state / 'server-data').mkdir()
    (state / 'server-data/quic-identity.pem').write_bytes(b'private identity fixture')
    if policy == 'protected':
        store = state / 'server-data/access-tokens'
        store.mkdir()
        # Opaque native state: the updater never interprets individual token data.
        (store / 'access-tokens.snapshot').write_bytes(b'opaque token/audit/outbox fixture')
    return state


class StatusValidation(unittest.TestCase):
    def test_initialized_zero_revision_and_pending_routes_are_healthy(self):
        for policy in ('open', 'protected'):
            for revision in ('0', str(2**64 - 1)):
                for pending in (0, 32, 33, 1024):
                    value = status(policy, revision=revision, pendingRouteSync=pending)
                    self.assertEqual(d.validate_access_status(value, IDENTITY, policy), value)

    def test_absent_store_valid_only_for_explicit_open_policy(self):
        self.assertEqual(d.validate_access_status(absent(), IDENTITY, 'open'), absent())
        for value, policy in ((absent(), 'protected'), (dict(absent(), revision='0'), 'open'),
                              (dict(absent(), policy='protected'), 'protected')):
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                d.validate_access_status(value, IDENTITY, policy)

    def test_schema_identity_integrity_revision_and_bounds_fail_closed(self):
        bad = [dict(status(), **{key: value}) for key, values in {
            'state': ['unknown', None], 'schemaVersion': [True, 1.0, '1', 2],
            'serverIdentity': ['b' * 64, None], 'policy': ['open', None],
            'integrity': ['failed', None], 'durability': ['indeterminate', None],
            'revision': [0, True, '', '00', '+1', '-1', '1.0', str(2**64)],
            'pendingRouteSync': [-1, 1025, True, 1.0, '0']}.items() for value in values]
        bad += [dict(status(), extra='unexpected')]
        for key in status():
            value = status()
            del value[key]
            bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                d.validate_access_status(value, IDENTITY, 'protected')

    def test_strict_json_rejects_duplicate_nonfinite_and_trailing_data(self):
        for raw in (b'{"revision":"1","revision":"2"}', b'{"x":{"a":1,"a":2}}',
                    b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'{}{}', b'\xff', b'x' * 32769):
            with self.subTest(raw=raw[:50]), self.assertRaises(d.Rejected):
                d.access_json(raw)

    def exchange(self, wire, policy='protected'):
        incoming = io.BytesIO(wire)
        with patch.object(d, 'admin_connection') as connection, patch.object(d.secrets, 'token_hex', return_value=REQUEST_ID):
            client = connection.return_value.__enter__.return_value
            client.recv.side_effect = incoming.read
            result = d.admin_access_status(123, IDENTITY, policy, Path('/fixture/admin.sock'))
            self.assertEqual(connection.call_args.args[1:], (123, Path('/fixture/admin.sock')))
            request = connection.call_args.args[0]
            self.assertTrue(request.startswith('ATRINIK-ADMIN/1 ACCESS '))
            self.assertEqual(json.loads(request.split(' ACCESS ', 1)[1]),
                             {'schema': 'atrinik-access-admin-v1', 'operation': 'status', 'requestId': REQUEST_ID})
            return result

    def test_framed_live_status_binds_both_initialized_and_absent_open(self):
        for pending in (0, 33, 1024):
            value = status(pendingRouteSync=pending)
            self.assertEqual(self.exchange(frame(envelope(value))), value)
        self.assertEqual(self.exchange(frame(envelope(absent())), 'open'), absent())

    def test_online_header_body_and_envelope_ambiguity_rejected(self):
        good = frame(envelope(status()))
        invalid = [good[:-1], good + b'\n', good + good, b'',
                   b'ATRINIK-ADMIN/1 ACCESS 0\n', b'ATRINIK-ADMIN/1 ACCESS 01\n{}',
                   b'ATRINIK-ADMIN/1 ACCESS 32769\n', b'ATRINIK-ADMIN/2 ACCESS 2\n{}',
                   b'ATRINIK-ADMIN/1 ACCESS 999999\n', b'garbage\n{}']
        for key, value in [('schema', 'other'), ('operation', 'issue'), ('requestId', 'c' * 32),
                           ('outcome', 'unavailable'), ('revision', '1'), ('revision', 0), ('extra', True)]:
            invalid.append(frame(dict(envelope(status()), **{key: value})))
        for wire in invalid:
            with self.subTest(wire=wire[:70]), self.assertRaises(d.Rejected):
                self.exchange(wire)
        for revision in ('0', 0, False):
            with self.subTest(absent_revision=revision), self.assertRaises(d.Rejected):
                self.exchange(frame(dict(envelope(absent()), revision=revision)), 'open')


class PolicyAndExecution(unittest.TestCase):
    def test_legacy_and_ambiguous_policy_or_external_paths_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config'
            bad = ['[meta]\n', '[meta]\naccess_required=yes\n',
                   '[meta]\naccess_required=true\naccess_initialize=true\n']
            for option in ('join_password=', 'join_password_file=', 'rendezvous_invite_file=',
                           'access_store=', 'access_store=/external', 'access_admin_accounts=',
                           'access_admin_accounts=/external',
                           'access_admin_accounts=/opt/atrinik/server/access-admin-accounts'):
                bad.append('[meta]\naccess_required=true\n' + option + '\n')
            for value in bad:
                path.write_text(value)
                with self.subTest(value=value), self.assertRaises(d.Rejected):
                    d.validate_config(path)
            path.write_text('[meta]\naccess_required=true\naccess_store=/opt/atrinik/server/data/access-tokens\n')
            self.assertEqual(d.validate_config(path), {'policy': 'protected'})

    def test_missing_store_and_dangling_symlinks_never_become_valid_protected_or_open(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'private'):
            state = fixture(Path(temp), 'open')
            self.assertEqual(d.access_paths(state)['policy'], 'open')
            store = state / 'server-data/access-tokens'
            store.symlink_to(state / 'missing')
            with self.assertRaisesRegex(d.Rejected, 'symlink'):
                d.access_paths(state)
            store.unlink()
            (state / 'config/server-custom.cfg').write_text('[meta]\naccess_required=true\n')
            with self.assertRaisesRegex(d.Rejected, 'store missing'):
                d.access_paths(state)

    def test_offline_inspector_is_provenance_checked_readonly_and_network_isolated(self):
        events = []
        pin = {'image_id': 'sha256:' + 'c' * 64}
        with patch.object(d, 'inspect_image', side_effect=lambda p: events.append('inspect')), patch.object(d, 'verify_provenance', side_effect=lambda p: events.append('provenance')), patch.object(d, 'command', side_effect=lambda *a, **kw: events.append('execute') or json.dumps(status())) as command:
            self.assertEqual(d.offline_access_status(Path('/fixture'), pin, IDENTITY, 'protected'), status())
        self.assertEqual(events, ['inspect', 'provenance', 'execute'])
        args = command.call_args.args
        for option, value in (('--network', 'none'), ('--user', '10001:10001'), ('--pull', 'never'), ('--platform', 'linux/amd64')):
            self.assertEqual(args[args.index(option) + 1], value)
        self.assertIn('--read-only', args)
        self.assertEqual(args.count('--mount'), 1)
        self.assertEqual(args[args.index('--mount') + 1], 'type=bind,src=/fixture/server-data,dst=/opt/atrinik/server/data,readonly')
        self.assertEqual(args[args.index('--entrypoint') + 1], '/opt/atrinik/server/atrinik-access-status')
        self.assertFalse(any('initialize' in arg or 'issue' in arg or 'docker.sock' in arg for arg in args))
        with patch.object(d, 'inspect_image'), patch.object(d, 'verify_provenance', side_effect=d.Rejected('unverified')), patch.object(d, 'command') as execute, self.assertRaises(d.Rejected):
            d.offline_access_status(Path('/fixture'), pin, IDENTITY, 'protected')
        execute.assert_not_called()

    def test_running_status_uses_exact_runtime_and_peer_no_offline_fallback(self):
        with patch.object(d, 'STATE', Path('/fixture')), patch.object(d, 'access_paths', return_value={'policy': 'protected'}), patch.object(d, 'command', side_effect=['container', '[{"State":{"Pid":321}}]']), patch.object(d, 'validate_runtime', side_effect=d.Rejected('wrong runtime')), patch.object(d, 'admin_request') as request, patch.object(d, 'offline_access_status') as offline, self.assertRaises(d.Rejected):
            d.validate_access(IDENTITY, pin={})
        request.assert_not_called()
        offline.assert_not_called()
        with patch.object(d, 'STATE', Path('/fixture')), patch.object(d, 'access_paths', return_value={'policy': 'protected'}), patch.object(d, 'command', side_effect=['container', '[{"State":{"Pid":321}}]']), patch.object(d, 'validate_runtime'), patch.object(d, 'admin_request', return_value='ATRINIK-ADMIN/1 CAPABILITIES shutdown-v1 durable-result-v1'), patch.object(d, 'admin_access_status') as access, self.assertRaisesRegex(d.Rejected, 'capability missing'):
            d.validate_access(IDENTITY, pin={})
        access.assert_not_called()

    def test_valid_protected_status_never_interprets_or_changes_individual_expiry(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'private'):
            state = fixture(Path(temp))
            snapshot = state / 'server-data/access-tokens/access-tokens.snapshot'
            for opaque in (b'empty initialized store fixture', b'fully expired store fixture', b'never-expiring store fixture', b'revoked entries and pending outbox fixture'):
                snapshot.write_bytes(opaque)
                with patch.object(d, 'command', return_value=''), patch.object(d, 'offline_access_status', return_value=status()) as inspect:
                    self.assertEqual(d.validate_access(IDENTITY, state, {'image_id': 'fixture'}, 'fixture'), status())
                self.assertEqual(snapshot.read_bytes(), opaque)
                inspect.assert_called_once_with(state, {'image_id': 'fixture'}, IDENTITY, 'protected')

    def test_obsolete_account_file_and_dangling_symlink_fail_before_execution(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), tempfile.TemporaryDirectory() as temp, patch.object(d, 'private'):
                state = fixture(Path(temp))
                obsolete = state / 'config/access-admin-accounts'
                if dangling:
                    obsolete.symlink_to(state / 'missing')
                else:
                    obsolete.write_text('')
                with patch.object(d, 'command') as command, patch.object(d, 'offline_access_status') as inspect, self.assertRaisesRegex(d.Rejected, 'obsolete'):
                    d.validate_access(IDENTITY, state, {'image_id': 'fixture'}, 'fixture')
                command.assert_not_called()
                inspect.assert_not_called()

    def test_runtime_args_never_mount_obsolete_account_file(self):
        with tempfile.TemporaryDirectory() as temp:
            state = fixture(Path(temp))
            (state / 'config/access-admin-accounts').write_text('')
            args = d.container_args('fixture', state, {'image_id': 'fixture'}, True)
            self.assertFalse(any('access-admin-accounts' in arg for arg in args))

    def test_runtime_contract_rejects_obsolete_account_mount(self):
        pin = {'image_id': 'sha256:' + 'a' * 64,
               'index_image': 'fixture@sha256:' + 'b' * 64}
        mounts = [{'Type': 'bind', 'Destination': target, 'Source': source, 'RW': rw}
                  for target, source, rw in (
                      ('/opt/atrinik/server/data', str(d.STATE / 'server-data'), True),
                      ('/opt/atrinik/server/server-custom.cfg', str(d.STATE / 'config/server-custom.cfg'), False),
                      (str(d.ADMIN.parent), str(d.ADMIN.parent), True))]
        runtime = {'Platform': 'linux', 'Image': pin['image_id'],
                   'Config': {'Image': pin['image_id'], 'User': '10001:10001',
                              'Labels': {'org.atrinik.development.managed': 'true'}},
                   'HostConfig': {'NetworkMode': 'host'}, 'Mounts': mounts}
        d.validate_runtime(runtime, pin)
        mounts.append({'Type': 'bind', 'Destination': '/opt/atrinik/server/access-admin-accounts',
                       'Source': str(d.STATE / 'config/access-admin-accounts'), 'RW': False})
        with self.assertRaisesRegex(d.Rejected, 'mount mismatch'):
            d.validate_runtime(runtime, pin)


class AuthorizationPreservation(unittest.TestCase):
    def test_rollback_refuses_newer_revocation_before_image_load_or_state_rename(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'private'):
            root = Path(temp)
            state = fixture(root)
            old = d.access_footprint(state)
            snapshot = state / 'server-data/access-tokens/access-tokens.snapshot'
            snapshot.write_bytes(b'newer durable revocation and audit')
            manifest = {'sha256': 'archive', 'image_sha256': 'image', 'access_state': old}
            with patch.object(d, 'STATE', state), patch.object(d, 'stopped'), patch.object(d, 'load', return_value=manifest), patch.object(d, 'file_hash', side_effect=lambda p: 'archive' if p.name == 'cohort.tar' else 'image' if p.name == 'image.tar' else __import__('hashlib').sha256(p.read_bytes()).hexdigest()), patch.object(d, 'command') as command, patch.object(Path, 'rename') as rename, self.assertRaisesRegex(d.Rejected, 'authorization changed'):
                d.restore(root / 'backup')
            command.assert_not_called()
            rename.assert_not_called()
            self.assertEqual(snapshot.read_bytes(), b'newer durable revocation and audit')

    def test_clone_covers_complete_authorization_store_and_retains_mutated_copy(self):
        for mutate in (False, True):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temp, patch.object(d, 'private'), patch.object(d.os, 'chown'):
                root = Path(temp)
                state = fixture(root)
                for name in ('accounts', 'players', 'unique-items'):
                    (state / 'server-data' / name).mkdir()
                player = state / 'server-data/players/fixture'
                player.write_bytes(b'cmd_permission access\n')
                backup = root / 'backup'
                backup.mkdir()
                subprocess.run(['tar', '-cpf', str(backup / 'cohort.tar'), '-C', '/', str(state).lstrip('/')], check=True)
                manifest = {'identity': IDENTITY, 'access_state': d.access_footprint(state)}
                clone_state = backup / 'clone' / str(state).lstrip('/')
                real_command = d.command
                def command(*args, **kwargs):
                    if args[0] == 'tar':
                        return real_command(*args, **kwargs)
                    if args[:2] == ('docker', 'run') and mutate:
                        (clone_state / 'server-data/access-tokens/access-tokens.snapshot').write_bytes(b'changed audit or grant')
                    if args[:2] == ('docker', 'inspect'):
                        return '[{"State":{"ExitCode":0}}]'
                    return ''
                with patch.object(d, 'STATE', state), patch.object(d, 'load', return_value=manifest), patch.object(d, 'identity', return_value=IDENTITY), patch.object(d, 'offline_access_status', return_value=status()), patch.object(d, 'validate_access', return_value=status()), patch.object(d, 'healthy'), patch.object(d, 'admin_stop'), patch.object(d, 'command', side_effect=command):
                    if mutate:
                        with self.assertRaisesRegex(d.Rejected, 'changed access'):
                            d.clone_check(backup, {'image_id': 'fixture'})
                        self.assertTrue(clone_state.exists())
                    else:
                        d.clone_check(backup, {'image_id': 'fixture'})
                        self.assertFalse((backup / 'clone').exists())
                self.assertEqual(player.read_bytes(), b'cmd_permission access\n')
                self.assertEqual((state / 'server-data/access-tokens/access-tokens.snapshot').read_bytes(), b'opaque token/audit/outbox fixture')


if __name__ == '__main__':
    unittest.main()
