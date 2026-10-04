import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import socket
import struct
import subprocess
from unittest.mock import patch, DEFAULT

SPEC = importlib.util.spec_from_file_location('development', Path(__file__).parents[1] / 'adapters/classic.py')
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)
d.MACHINE = '1' * 32
d.UUID = '11111111-1111-4111-8111-111111111111'
d.DISK_SERIAL = 'FIXTURE_DISK'
d.PRODUCTION_ID = '1' * 64


def pin(version='5.76.0', digest='a', release=101):
    return {'version': version, 'image': d.IMAGE + '@sha256:' + digest * 64, 'index_image': d.IMAGE + '@sha256:' + 'f' * 64,
            'revision': 'b' * 40, 'release_id': release, 'image_id': 'sha256:' + digest * 64, 'config_digest': 'sha256:' + digest * 64}


class Releases(unittest.TestCase):
    def test_reject_bad_stable_versions(self):
        for value in ('5.76', '5.76.0;id', '5.76.0-rc1', '05.76.0', '', None):
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                d.version(value)

    def test_release_requires_official_stable_publication(self):
        release = {'draft': False, 'prerelease': False, 'tag_name': 'v5.76.0', 'id': 101,
                   'html_url': 'https://github.com/atrinik/classic/releases/tag/v5.76.0',
                   'published_at': '2026-10-01T00:00:00Z'}
        self.assertEqual(d.validate_release(release), 'v5.76.0')
        for key, value in [('draft', True), ('prerelease', True), ('id', True),
                           ('html_url', 'https://example.org/release'), ('published_at', '2999-01-01T00:00:00Z')]:
            with self.subTest(key=key), self.assertRaises(d.Rejected):
                d.validate_release(dict(release, **{key: value}))

    def test_downgrade_and_retag_rejected(self):
        ledger = {'accepted': [pin()]}
        for candidate in (pin('5.75.0', release=99), pin(digest='c'), pin(release=102),
                          pin('5.77.0', release=101)):
            with self.subTest(candidate=candidate), self.assertRaises(d.Rejected):
                d.check_monotonic(candidate, ledger)
        d.check_monotonic(pin(), ledger)
        d.check_monotonic(pin('5.77.0', release=102), ledger)

    def test_unsafe_image_reference_rejected(self):
        for image in (d.IMAGE + ':latest', 'evil.example/image@sha256:' + 'a' * 64,
                      d.IMAGE + '@sha256:' + 'A' * 64):
            with self.subTest(image=image), self.assertRaises(d.Rejected):
                d.validate_pin(dict(pin(), image=image))

    def test_platform_and_oci_provenance(self):
        good = {'Id': pin()['image_id'], 'Architecture': 'amd64', 'Os': 'linux', 'RepoDigests': [pin()['image']],
                'Config': {'Labels': {'org.opencontainers.image.revision': 'b' * 40,
                                     'org.opencontainers.image.version': '5.76.0',
                                     'org.opencontainers.image.source': 'https://github.com/atrinik/classic'}}}
        with patch.object(d, 'command', return_value=json.dumps([good])):
            d.inspect_image(pin())
        good['Architecture'] = 'arm64'
        with patch.object(d, 'command', return_value=json.dumps([good])), self.assertRaises(d.Rejected):
            d.inspect_image(pin())


class Safety(unittest.TestCase):
    def test_candidate_is_network_isolated_and_uses_complete_clone(self):
        args = d.container_args('candidate', Path('/private/clone'), pin(), True)
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('ATRINIK_SERVER_PUBLIC=false', args)
        self.assertIn('type=bind,src=/private/clone/server-data,dst=/opt/atrinik/server/data', args)
        self.assertIn('type=bind,src=/private/clone/config/server-custom.cfg,dst=/opt/atrinik/server/server-custom.cfg,readonly', args)
        self.assertIn('ALL', args)
        self.assertIn('no-new-privileges:true', args)

    def test_wrong_admin_peer_is_rejected_before_request(self):
        with patch.object(d, 'private'), patch.object(Path, 'lstat') as info, patch.object(d.socket, 'socket') as socket_factory:
            info.return_value.st_mode = d.stat.S_IFSOCK | 0o600
            info.return_value.st_uid = 10001
            client = socket_factory.return_value.__enter__.return_value
            client.getsockopt.return_value = struct.pack('3i', 999, 10001, 10001)
            with self.assertRaises(d.Rejected):
                d.admin_request('ATRINIK-ADMIN/1 CAPABILITIES', 123)
            client.sendall.assert_not_called()

    def test_missing_live_admin_has_no_signal_fallback(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            return json.dumps([{'Id': 'fixed', 'State': {'Pid': 123}}]) if args[:2] == ('docker', 'inspect') else 'running'
        with patch.object(d, 'validate_runtime'), patch.object(d, 'load', return_value=pin()), patch.object(d, 'command', side_effect=command), patch.object(d, 'admin_request', side_effect=FileNotFoundError), self.assertRaises(FileNotFoundError):
            d.admin_stop()
        self.assertFalse(any('kill' in c or 'stop' in c for c in calls))

    def test_saved_result_required_even_with_exit_zero(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('docker', 'inspect'):
                return json.dumps([{'Id': 'fixed', 'State': {'Pid': 123, 'Running': False, 'ExitCode': 0}}])
            return 'running'
        with patch.object(d, 'validate_runtime'), patch.object(d, 'load', return_value=pin()), patch.object(d, 'command', side_effect=command), patch.object(d, 'admin_request', side_effect=['ATRINIK-ADMIN/1 CAPABILITIES shutdown-v1 durable-result-v1', 'ATRINIK-ADMIN/1 SCHEDULED ' + 'a' * 32]), patch.object(d.secrets, 'token_hex', return_value='a' * 32), patch.object(Path, 'exists', return_value=False), patch.object(d, 'private'), patch.object(Path, 'read_text', return_value='ATRINIK-ADMIN/1 RESULT ' + 'a' * 32 + ' failed\n'), self.assertRaises(d.Rejected):
            d.admin_stop()
        self.assertFalse(any('kill' in c for c in calls))

    def test_direct_endpoint_and_unprotected_config_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config'
            for text in ('[meta]\njoin_password=\n', '[meta]\naccess_required=true\nmetaserver_hostname=example.org\n'):
                config.write_text(text)
                with self.assertRaises(d.Rejected):
                    d.validate_config(config)
            config.write_text('[meta]\naccess_required=true\nmetaserver_hostname=\n')
            d.validate_config(config)

    def test_protected_save_fingerprints_detect_modification_and_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            for name in ('accounts', 'players', 'unique-items'):
                (state / 'server-data' / name).mkdir(parents=True)
            player = state / 'server-data/players/player.dat'
            private_map = state / 'server-data/players/private$map'
            player.write_bytes(b'player inventory and map reference')
            private_map.write_bytes(b'legacy map objects')
            before = d.protected_saves(state)
            player.write_bytes(b'truncated')
            self.assertNotEqual(before, d.protected_saves(state))
            private_map.unlink()
            self.assertNotEqual(before.keys(), d.protected_saves(state).keys())

    def test_private_map_acceptance_cannot_be_skipped_or_downgraded(self):
        acceptance = {'private_map_roundtrip': True, 'private_map_count': 5,
                      'minimum_version': '5.77.0', 'source_revision': 'b' * 40}
        with patch.object(d, 'load', return_value=acceptance):
            with self.assertRaises(d.Rejected):
                d.accepted_compatibility(pin())
            d.accepted_compatibility(pin('5.77.0'))
        acceptance['private_map_roundtrip'] = False
        with patch.object(d, 'load', return_value=acceptance), self.assertRaises(d.Rejected):
            d.accepted_compatibility(pin('5.77.0'))

    def test_stop_handoff_relocks_and_rechecks_changed_transaction(self):
        candidate = pin('5.77.0', release=102)
        def load(path):
            if path.name == 'policy.json':
                return {'activation_enabled': True}
            if path.name == 'pin.json':
                return pin()
            if path.name == 'transaction.json':
                return {'phase': 'changed'}
            return {'accepted': [pin()]}
        with patch.multiple(d, accepted_compatibility=DEFAULT, inspect_image=DEFAULT, verify_provenance=DEFAULT, image_capability=DEFAULT, headroom=DEFAULT, admin_stop=DEFAULT, fence=DEFAULT, guard=DEFAULT, atomic=DEFAULT), patch.object(d, 'command', return_value=''), patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.object(d.fcntl, 'flock') as flock, patch.object(d, 'archive') as archive, self.assertRaises(d.Rejected):
            d.activate(candidate, lock=12)
        self.assertEqual([call.args for call in flock.call_args_list], [(12, d.fcntl.LOCK_UN), (12, d.fcntl.LOCK_EX)])
        archive.assert_not_called()

    def test_wrong_filesystem_and_running_production_fail_closed(self):
        def read(path, *args, **kwargs):
            if str(path) == '/etc/machine-id':
                return d.MACHINE
            return 'ConditionPathExists=!/etc/atrinik-production-migrated'
        def command(*args, **kwargs):
            if args[0] == 'busctl':
                if 'GetUnit' in args:
                    return json.dumps({'type': 'o', 'data': ['/org/freedesktop/systemd1/unit/atrinik_2druntime_2eservice']})
                if args[-1] == 'Id':
                    return json.dumps({'type': 's', 'data': d.PRODUCTION_SERVICE})
                return json.dumps({'type': 'a(sbbsi)', 'data': [['ConditionPathExists', False, True, str(d.PRODUCTION_MARKER), 0]]})
            if '--property=ActiveState' in args:
                return 'inactive'
            if args[0] == 'findmnt':
                return json.dumps({'filesystems': [{'source': '/dev/vdb', 'uuid': 'WRONG'}]})
            return ''
        with patch.object(d.os, 'geteuid', return_value=0), patch.object(Path, 'read_text', autospec=True, side_effect=read), patch.object(d, 'private'), patch.object(d, 'command', side_effect=command), self.assertRaisesRegex(d.Rejected, 'wrong state filesystem'):
            d.fence(False)
        def running(*args, **kwargs):
            return 'production-container' if args[:2] == ('docker', 'ps') else command(*args, **kwargs)
        with patch.object(d.os, 'geteuid', return_value=0), patch.object(Path, 'read_text', autospec=True, side_effect=read), patch.object(d, 'private'), patch.object(d, 'command', side_effect=running), self.assertRaisesRegex(d.Rejected, 'production container running'):
            d.fence(False)

    def test_real_tar_restore_preserves_failed_state_and_restores_full_cohort(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state, runtime, backup = root / 'development', root / 'runtime', root / '20260101T000000Z'
            for path in (state / 'server-data/players', state / 'config', runtime, backup):
                path.mkdir(parents=True)
            save = state / 'server-data/players/player.dat'
            config = state / 'config/server-custom.cfg'
            save.write_bytes(b'original player inventory')
            config.write_bytes(b'[meta]\naccess_required=false\n')
            (state / 'server-data/quic-identity.pem').write_bytes(b'certificate fixture')
            (runtime / 'pin.json').write_text(json.dumps(pin()))
            subprocess.run(['tar', '--format=pax', '--acls', '--xattrs', '--numeric-owner', '-cpf', str(backup / 'cohort.tar'), '-C', '/', str(state).lstrip('/'), str(runtime).lstrip('/')], check=True)
            (backup / 'image.tar').write_bytes(b'archived exact image fixture')
            (backup / 'manifest.json').write_text(json.dumps({'sha256': d.file_hash(backup / 'cohort.tar'), 'image_sha256': d.file_hash(backup / 'image.tar'), 'identity': 'fixture', 'pin': pin(), 'access_state': {'config/server-custom.cfg': d.file_hash(config), 'server-data/quic-identity.pem': d.file_hash(state / 'server-data/quic-identity.pem')}}))
            save.write_bytes(b'failed candidate writes')
            (runtime / 'pin.json').write_text(json.dumps(pin('5.77.0', release=102)))
            real_command = d.command
            def command(*args, **kwargs):
                return '' if args[:3] == ('docker', 'image', 'load') else real_command(*args, **kwargs)
            with patch.object(d, 'STATE', state), patch.object(d, 'ROOT', runtime), patch.object(d, 'stopped'), patch.object(d, 'private'), patch.object(d, 'identity', return_value='fixture'), patch.object(d, 'inspect_image'), patch.object(d, 'load', side_effect=lambda path: json.loads(path.read_text())), patch.object(d, 'command', side_effect=command):
                d.restore(backup)
            self.assertEqual(save.read_bytes(), b'original player inventory')
            self.assertEqual(config.read_bytes(), b'[meta]\naccess_required=false\n')
            self.assertEqual(json.loads((runtime / 'pin.json').read_text()), pin())
            failed = state.with_name('development-failed-' + backup.name)
            self.assertEqual((failed / 'server-data/players/player.dat').read_bytes(), b'failed candidate writes')

    def test_retention_separates_failed_and_active_clones_from_completed_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            backups = Path(temp)
            paths = []
            for day in range(1, 11):
                path = backups / f'202601{day:02d}T000000Z'
                path.mkdir()
                paths.append(path)
                if day == 1:
                    continue  # Incomplete oldest archive must not displace first success.
                (path / 'cohort.tar').write_bytes(b'cohort')
                (path / 'image.tar').write_bytes(b'image')
                (path / 'manifest.json').write_text(json.dumps({'sha256': d.file_hash(path / 'cohort.tar'), 'image_sha256': d.file_hash(path / 'image.tar')}))
                (path / 'completed.json').write_text(json.dumps({'backup': path.name}))
            (paths[2] / 'clone').mkdir()  # Never remove a retained candidate bind root.
            with patch.object(d, 'BACKUPS', backups), patch.object(d, 'private'), patch.object(d.core, 'private'):
                d.retain(paths[-1])
            self.assertTrue(paths[0].exists())
            self.assertTrue(paths[1].exists())
            self.assertTrue(paths[2].exists())
            self.assertFalse(paths[3].exists())
            self.assertTrue(all(p.exists() for p in paths[4:]))
            self.assertEqual(sum(p.exists() for p in paths), 9)

    def test_invalid_access_store_refuses_update_before_shutdown_or_writes(self):
        with patch.object(d, 'fence', side_effect=d.Rejected('invalid access store')), patch.object(d, 'admin_stop') as stop, patch.object(d, 'guard') as guard, patch.object(d, 'atomic') as write, self.assertRaises(d.Rejected):
            d.activate(pin('5.77.0', release=102))
        stop.assert_not_called()
        guard.assert_not_called()
        write.assert_not_called()

    def test_wrong_machine_fails_before_any_command(self):
        with patch.object(d.os, 'geteuid', return_value=0), patch.object(Path, 'read_text', return_value='wrong'), patch.object(d, 'command') as cmd, self.assertRaises(d.Rejected):
            d.fence()
        cmd.assert_not_called()

    def test_active_runtime_cannot_be_archived(self):
        with patch.object(d, 'command', return_value='active'), self.assertRaises(d.Rejected):
            d.archive()

    def test_stop_timeout_never_uses_sigkill_or_docker_stop(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            return json.dumps([{'State': {'Running': True}}]) if args[:2] == ('docker', 'inspect') else ''
        with patch.object(d, 'command', side_effect=command), patch.object(d.time, 'sleep'), self.assertRaises(d.Rejected):
            d.graceful_stop('game')
        self.assertIn(('docker', 'kill', '--signal', 'TERM', 'game'), calls)
        self.assertFalse(any('KILL' in c or 'stop' in c or '--force' in c for c in calls))

    def test_failed_clone_does_not_change_live_pin(self):
        candidate = pin('5.77.0', release=102)
        def load(path):
            if path.name == 'policy.json':
                return {'activation_enabled': True}
            return pin() if path.name == 'pin.json' else {'accepted': [pin()]}
        with patch.multiple(d, accepted_compatibility=DEFAULT, validate_access=DEFAULT, validate_runtime=DEFAULT, image_capability=DEFAULT, require_admin=DEFAULT, graceful_stop=DEFAULT, inspect_image=DEFAULT, verify_provenance=DEFAULT, headroom=DEFAULT, launch=DEFAULT, admin_stop=DEFAULT, fence=DEFAULT, stopped=DEFAULT), patch.object(d, 'command', side_effect=lambda *args, **kwargs: '[{}]' if args[:2] == ('docker', 'inspect') else ''), patch.object(d, 'guard'), patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.object(d, 'archive', return_value=Path('/backup')), patch.object(d, 'clone_check', side_effect=d.Rejected('failed migration')), patch.object(d, 'atomic') as write, self.assertRaises(d.Rejected):
            d.activate(candidate)
        self.assertTrue(write.called)
        self.assertTrue(all(call.args[0].name == 'transaction.json' for call in write.call_args_list))

    def test_preopen_activation_failure_restores_complete_cohort(self):
        candidate = pin('5.77.0', release=102)
        def load(path):
            if path.name == 'policy.json':
                return {'activation_enabled': True}
            return pin() if path.name == 'pin.json' else {'accepted': [pin()]}
        with patch.multiple(d, accepted_compatibility=DEFAULT, validate_access=DEFAULT, validate_runtime=DEFAULT, image_capability=DEFAULT, require_admin=DEFAULT, graceful_stop=DEFAULT, inspect_image=DEFAULT, verify_provenance=DEFAULT, headroom=DEFAULT, launch=DEFAULT, admin_stop=DEFAULT, fence=DEFAULT, stopped=DEFAULT), patch.object(d, 'command', side_effect=lambda *args, **kwargs: '[{}]' if args[:2] == ('docker', 'inspect') else ''), patch.object(d, 'guard') as guard, patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.object(d, 'archive', return_value=Path('/backup')), patch.object(d, 'clone_check'), patch.object(d, 'healthy', side_effect=d.Rejected('health failed')), patch.object(d, 'atomic'), patch.object(d, 'restore') as restore, self.assertRaises(d.Rejected):
            d.activate(candidate)
        restore.assert_called_once_with(Path('/backup'))
        self.assertNotIn(('public',), [call.args for call in guard.call_args_list])

    def test_public_reopen_failure_never_rolls_back_saves(self):
        candidate = pin('5.77.0', release=102)
        def load(path):
            if path.name == 'policy.json':
                return {'activation_enabled': True}
            if path.name == 'pin.json':
                return pin()
            if path.name == 'manifest.json':
                return {'identity': 'dev'}
            return {'accepted': [pin()]}
        def guard(mode):
            if mode == 'public':
                raise d.Rejected('uncertain public state')
        with patch.multiple(d, accepted_compatibility=DEFAULT, validate_access=DEFAULT, validate_runtime=DEFAULT, image_capability=DEFAULT, require_admin=DEFAULT, graceful_stop=DEFAULT, inspect_image=DEFAULT, verify_provenance=DEFAULT, headroom=DEFAULT, launch=DEFAULT, admin_stop=DEFAULT, fence=DEFAULT, stopped=DEFAULT), patch.object(d, 'command', side_effect=lambda *args, **kwargs: '[{}]' if args[:2] == ('docker', 'inspect') else ''), patch.object(d, 'guard', side_effect=guard), patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.object(d, 'archive', return_value=Path('/backup')), patch.object(d, 'clone_check'), patch.object(d, 'healthy'), patch.object(d, 'identity', return_value='dev'), patch.object(d, 'atomic') as writes, patch.object(d, 'restore') as restore, self.assertRaises(d.Rejected):
            d.activate(candidate)
        restore.assert_not_called()
        self.assertEqual(writes.call_args_list[-1].args[1]['phase'], 'may-have-played')

    def test_restore_preserves_failed_saves_and_rejects_corrupt_backup(self):
        with patch.object(d, 'stopped'), patch.object(d, 'load', return_value={'sha256': 'expected'}), patch.object(d, 'file_hash', return_value='wrong'), patch.object(Path, 'rename') as rename, self.assertRaises(d.Rejected):
            d.restore(Path('/backup'))
        rename.assert_not_called()

    def test_symlink_and_world_readable_state_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'config'
            target.write_text('secret')
            target.chmod(0o644)
            with self.assertRaises(d.Rejected):
                d.private(target, target.stat().st_uid, modes=(0o600,))
            link = Path(temp) / 'link'
            link.symlink_to(target)
            with self.assertRaises(d.Rejected):
                d.private(link, target.stat().st_uid, modes=(0o644,))

if __name__ == '__main__':
    unittest.main()
