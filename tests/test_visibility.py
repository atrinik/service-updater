"""Native visibility reaches the live CLI without overriding explicit privacy."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters import classic as d


class Visibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / 'config').mkdir()
        self.config = self.state / 'config/server-custom.cfg'
        self.pin = {'image_id': 'sha256:' + 'a' * 64, 'index_image': 'fixture@sha256:' + 'b' * 64}

    def write_config(self, public=None, access='true'):
        self.config.write_text('[meta]\naccess_required=' + access + '\n' +
                               ('' if public is None else 'server_public=' + public + '\n'))

    def visibility_env(self, argv):
        return [argv[index + 1] for index, argument in enumerate(argv[:-1])
                if argument == '--env' and argv[index + 1].startswith('ATRINIK_SERVER_PUBLIC=')]

    def runtime(self, env):
        return {'Platform': 'linux', 'Image': self.pin['image_id'],
                'Config': {'Image': self.pin['image_id'], 'User': '10001:10001', 'Env': env,
                           'Labels': {'org.atrinik.development.managed': 'true'}},
                'HostConfig': {'NetworkMode': 'host'},
                'Mounts': [{'Type': 'bind', 'Destination': target, 'Source': str(source), 'RW': rw}
                           for target, source, rw in (
                               ('/opt/atrinik/server/data', self.state / 'server-data', True),
                               ('/opt/atrinik/server/server-custom.cfg', self.config, False),
                               (str(d.ADMIN.parent), d.ADMIN.parent, True))]}

    def test_live_visibility_preserves_explicit_value_and_legacy_default_in_both_channels(self):
        for channel in ('stable', 'development'):
            for public in ('true', 'false', None):
                for access in ('true', 'false'):
                    with self.subTest(channel=channel, public=public, access=access), patch.object(d, 'RELEASE_CHANNEL', channel):
                        self.write_config(public, access)
                        args = d.container_args('live', self.state, self.pin, False)
                        self.assertEqual(self.visibility_env(args), ['ATRINIK_SERVER_PUBLIC=' + (public or 'true')])
                        self.assertEqual(args[args.index('--network') + 1], 'host')

    def test_launch_passes_private_environment_and_same_readonly_native_config(self):
        self.write_config('false')
        with patch.object(d, 'STATE', self.state), patch.object(d, 'private'), patch.object(d, 'command', return_value='') as command:
            d.launch(self.pin)
        args = command.call_args.args
        self.assertEqual(args[:2], ('docker', 'run'))
        self.assertEqual(self.visibility_env(args), ['ATRINIK_SERVER_PUBLIC=false'])
        self.assertIn('type=bind,src=' + str(self.config) + ',dst=/opt/atrinik/server/server-custom.cfg,readonly', args)
        self.assertEqual(args[-1], self.pin['image_id'])

    def test_isolated_candidates_are_private_even_for_explicit_public_config(self):
        for public in ('true', 'false', None):
            with self.subTest(public=public):
                self.write_config(public)
                args = d.container_args('candidate', self.state, self.pin, True)
                self.assertEqual(self.visibility_env(args), ['ATRINIK_SERVER_PUBLIC=false'])
                self.assertEqual(args[args.index('--network') + 1], 'none')

    def test_ambiguous_visibility_fails_before_docker_run(self):
        for setting in ('server_public="false"', "server_public='false'", 'server_public=0',
                        'server_public=no', 'server_public=False', 'server_public=',
                        'server_public=false # private', 'server_pub=false',
                        'server_public=false\nserver_public=true', '[general]\nserver_public=false'):
            self.config.write_text('[meta]\naccess_required=true\n' + setting + '\n')
            with self.subTest(setting=setting), self.assertRaises(d.Rejected):
                d.validate_config(self.config)
            with self.subTest(setting=setting), self.assertRaises(d.Rejected):
                d.container_args('live', self.state, self.pin, False)

    def test_retained_runtime_must_match_current_config_exactly(self):
        for public in ('true', 'false', None):
            self.write_config(public)
            expected = public or 'true'
            with self.subTest(public=public), patch.object(d, 'STATE', self.state):
                d.validate_runtime(self.runtime(['HOME=/tmp', 'ATRINIK_SERVER_PUBLIC=' + expected]), self.pin)
                for env in (None, [], ['ATRINIK_SERVER_PUBLIC'], ['ATRINIK_SERVER_PUBLIC=TRUE'],
                            ['ATRINIK_SERVER_PUBLIC=' + ('false' if expected == 'true' else 'true')],
                            ['ATRINIK_SERVER_PUBLIC=' + expected] * 2,
                            ['ATRINIK_SERVER_PUBLIC=' + expected, 'ATRINIK_SERVER_PUBLIC'], [None]):
                    with self.subTest(env=env), self.assertRaises(d.Rejected):
                        d.validate_runtime(self.runtime(env), self.pin)

    def test_stale_public_runtime_can_stop_but_wrong_runtime_still_rejects(self):
        self.write_config('false')
        for wrong_image in (False, True):
            with self.subTest(wrong_image=wrong_image):
                runtime = self.runtime(['ATRINIK_SERVER_PUBLIC=true'])
                runtime.update(Id='container-id', State={'Pid': 123, 'Running': True})
                if wrong_image:
                    runtime['Image'] = 'sha256:' + 'f' * 64
                stopped = dict(runtime, State={'Pid': 0, 'Running': False, 'ExitCode': 0})
                request_id = 'e' * 32
                with patch.object(d, 'STATE', self.state), patch.object(d, 'load', return_value=self.pin), patch.object(d, 'command', side_effect=['running', json.dumps([runtime]), json.dumps([stopped])]), patch.object(d, 'admin_request', side_effect=['ATRINIK-ADMIN/1 CAPABILITIES shutdown-v1 durable-result-v1', 'ATRINIK-ADMIN/1 SCHEDULED ' + request_id]) as request, patch.object(d.secrets, 'token_hex', return_value=request_id), patch.object(Path, 'exists', return_value=False), patch.object(d, 'private'), patch.object(Path, 'read_text', return_value='ATRINIK-ADMIN/1 RESULT ' + request_id + ' saved\n'):
                    if wrong_image:
                        with self.assertRaisesRegex(d.Rejected, 'unexpected existing runtime'):
                            d.admin_stop()
                        request.assert_not_called()
                    else:
                        d.admin_stop()
                        self.assertEqual(request.call_count, 2)
                        self.assertTrue(request.call_args.args[0].startswith('ATRINIK-ADMIN/1 SHUTDOWN '))
                        self.assertEqual(request.call_args.args[1], 123)

    def test_config_change_rejects_running_public_container_before_native_access_request(self):
        self.write_config('false')
        runtime = self.runtime(['ATRINIK_SERVER_PUBLIC=true'])
        with patch.object(d, 'STATE', self.state), patch.object(d, 'command', side_effect=['running', json.dumps([runtime])]), patch.object(d, 'access_paths', return_value={'policy': 'protected'}), patch.object(d, 'admin_request') as request, self.assertRaisesRegex(d.Rejected, 'visibility'):
            d.validate_access('c' * 64, state=self.state, pin=self.pin)
        request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
