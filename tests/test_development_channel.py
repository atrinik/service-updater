"""Channel trust boundary and source-order regression coverage."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import DEFAULT, patch

import core
from adapters import classic as d

IMAGE = 'ghcr.io/atrinik/classic-server'
WORKFLOW = 'atrinik/classic/.github/workflows/publish-development-server.yml'


def pin(revision='a', digest='b'):
    return {'release_channel': 'development', 'version': '0.0.0', 'revision': revision * 40,
            'image': IMAGE + '@sha256:' + digest * 64,
            'index_image': IMAGE + '@sha256:' + 'c' * 64,
            'config_digest': 'sha256:' + 'd' * 64, 'image_id': 'sha256:' + 'd' * 64}


def settings():
    return patch.multiple(d, RELEASE_CHANNEL='development', IMAGE=IMAGE,
                          SIGNER_WORKFLOW=WORKFLOW, SOURCE_REF='refs/heads/main', DISCOVERY_TAG='development')


def source():
    return core.DevelopmentSource('atrinik/example', 'ghcr.io/atrinik/example-development',
                                  'atrinik/example/.github/workflows/publish.yml', 'refs/heads/main', 'development')


class ChannelConfig(unittest.TestCase):
    def config(self):
        data = json.loads((Path(__file__).parents[1] / 'config/deployment-development.example.json').read_text())
        data.update(machine_id='a' * 32, state_filesystem_uuid='11111111-1111-4111-8111-111111111111',
                    production_identity_sha256='b' * 64, updater_image='ghcr.io/atrinik/service-updater@sha256:' + 'c' * 64)
        return data

    def test_schema2_explicit_and_canonical_tuple(self):
        config = self.config()
        self.assertEqual(d.validate_deployment(config)['RELEASE_CHANNEL'], 'development')
        for field in ('release_channel', 'source_ref', 'discovery_tag'):
            bad = dict(config)
            del bad[field]
            with self.subTest(field=field), self.assertRaises(core.Rejected):
                d.validate_deployment(bad)
        for key, value in [('schema_version', 1), ('release_channel', 'edge'), ('release_channel', 'stable'),
                           ('release_repository', 'evil/classic'), ('release_image', 'ghcr.io/evil/classic-server'),
                           ('signer_workflow', 'atrinik/classic/.github/workflows/package-release.yml'),
                           ('source_ref', 'refs/heads/other'), ('discovery_tag', 'latest')]:
            with self.subTest(key=key), self.assertRaises(core.Rejected):
                d.validate_deployment(dict(config, **{key: value}))

    def test_schema2_stable_preserves_release_contract(self):
        config = self.config()
        for key in ('source_ref', 'discovery_tag'):
            del config[key]
        config.update(release_channel='stable', release_image='ghcr.io/atrinik/classic-server',
                      signer_workflow='atrinik/classic/.github/workflows/package-release.yml')
        self.assertEqual(d.validate_deployment(config)['RELEASE_CHANNEL'], 'stable')

    def test_default_signer_resolves_current_configuration(self):
        with settings(), patch.object(d, 'VERIFIER_IMAGE', 'ghcr.io/atrinik/service-updater@sha256:' + 'e' * 64):
            args = d.verifier_args(Path('/tmp/public'), 'a' * 40)
        for flag, expected in [('--signer-workflow', WORKFLOW), ('--source-ref', 'refs/heads/main'),
                               ('--source-digest', 'a' * 40), ('--repo', 'atrinik/classic'),
                               ('--predicate-type', 'https://slsa.dev/provenance/v1')]:
            self.assertEqual(args[args.index(flag) + 1], expected)
        self.assertIn('--deny-self-hosted-runners', args)


class SourceOrdering(unittest.TestCase):
    def test_generic_helper_contains_no_classic_assumptions(self):
        candidate = {key: value.replace(IMAGE, 'ghcr.io/atrinik/example-development') if isinstance(value, str) else value
                     for key, value in pin().items()}
        source().validate_pin(candidate)

    def test_shared_image_keeps_disjoint_stable_and_development_tags(self):
        stable = d.source()
        with settings():
            development = d.source()
        for tag in ('development', 'source-' + 'a' * 40):
            self.assertFalse(stable.manifest_reference(tag))
            self.assertTrue(development.manifest_reference(tag))
        self.assertTrue(stable.manifest_reference('5.80.0'))
        self.assertFalse(development.manifest_reference('5.80.0'))
        for tag in ('latest', 'source-' + 'a' * 39, '../development'):
            self.assertFalse(development.manifest_reference(tag))

    def test_channel_image_version_and_fake_release_identity_rejected(self):
        with settings():
            d.validate_pin(pin())
            for key, value in [('release_channel', 'stable'), ('version', '5.80.0'), ('release_id', 101),
                               ('revision', 'a' * 39), ('image', 'ghcr.io/evil/classic-server@sha256:' + 'b' * 64)]:
                with self.subTest(key=key), self.assertRaises(core.Rejected):
                    d.validate_pin(dict(pin(), **{key: value}))
        with self.assertRaises(core.Rejected):
            d.validate_pin(pin())

    def test_every_accepted_revision_is_required_ancestor(self):
        accepted = [pin('a'), pin('e')]
        with settings(), patch.object(core.DevelopmentSource, 'api', side_effect=[
                {'status': 'ahead', 'merge_base_commit': {'sha': 'a' * 40}},
                {'status': 'ahead', 'merge_base_commit': {'sha': 'e' * 40}}]) as api:
            d.check_monotonic(pin('f'), {'accepted': accepted})
        self.assertEqual([call.args[0] for call in api.call_args_list],
                         ['/compare/' + rev * 40 + '...' + 'f' * 40 for rev in ('a', 'e')])

    def test_rollback_divergence_reused_sha_and_mixed_ledgers_fail(self):
        with settings():
            for comparison in [{'status': 'behind'}, {'status': 'diverged'},
                               {'status': 'ahead', 'merge_base_commit': {'sha': 'e' * 40}}]:
                with self.subTest(comparison=comparison), patch.object(core.DevelopmentSource, 'api', return_value=comparison), self.assertRaises(core.Rejected):
                    d.check_monotonic(pin('f'), {'accepted': [pin()]})
            with self.assertRaisesRegex(core.Rejected, 'reused'):
                d.check_monotonic(pin(digest='e'), {'accepted': [pin()]})
            with self.assertRaises(core.Rejected):
                d.check_monotonic(pin(), {'accepted': [dict(pin(), release_channel='stable')]})
            with patch.object(core.DevelopmentSource, 'api') as api:
                d.check_monotonic(pin(), {'accepted': [pin()]})
                api.assert_not_called()

    def test_source_tag_alias_race_and_wrong_source_tag_reject(self):
        with settings():
            for pair in [('c', 'e'), ('e', 'c')]:
                with patch.object(core.DevelopmentSource, 'manifest', side_effect=[('sha256:' + x * 64, b'{}') for x in pair]), self.assertRaisesRegex(core.Rejected, 'alias changed'):
                    d.source().check_discovery(pin())
            with patch.object(core.DevelopmentSource, 'manifest', return_value=('sha256:' + 'c' * 64, b'{}')) as manifest:
                d.source().check_discovery(pin())
                self.assertEqual([call.args[0] for call in manifest.call_args_list], ['source-' + 'a' * 40, 'development'])

    def test_retained_provenance_never_resolves_discovery_or_source_tag(self):
        raw = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + 'b' * 64,
                         'platform': {'os': 'linux', 'architecture': 'amd64'}}]}).encode()
        child = json.dumps({'config': {'digest': 'sha256:' + 'd' * 64}}).encode()
        with settings(), patch.object(d, 'manifest', side_effect=[('index', raw), ('child', child)]) as manifest, patch.object(d, 'verify_index'):
            d.verify_provenance(pin())
        self.assertEqual([call.args[0] for call in manifest.call_args_list], ['sha256:' + x * 64 for x in ('c', 'b')])

    def test_private_map_acceptance_and_source_ancestry_remain_mandatory(self):
        acceptance = {'private_map_roundtrip': True, 'private_map_count': 5, 'source_revision': 'a' * 40,
                      'minimum_version': '5.80.0'}
        with settings(), patch.object(d, 'load', return_value=acceptance):
            d.accepted_compatibility(pin())  # 0.0.0 does not compare to stable versions.
            with patch.object(d, 'api', return_value={'status': 'diverged'}), self.assertRaises(core.Rejected):
                d.accepted_compatibility(pin('e'))
        with settings(), patch.object(d, 'load', return_value=dict(acceptance, private_map_roundtrip=False)), self.assertRaises(core.Rejected):
            d.accepted_compatibility(pin())


class StageAndRun(unittest.TestCase):
    def stage_context(self, stack):
        stack.enter_context(settings())
        raw = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + 'b' * 64,
                         'platform': {'os': 'linux', 'architecture': 'amd64'}}]}).encode()
        child = json.dumps({'config': {'digest': 'sha256:' + 'd' * 64}}).encode()
        stack.enter_context(patch.object(core.DevelopmentSource, 'manifest', side_effect=[('sha256:' + 'c' * 64, raw), ('sha256:' + 'b' * 64, child)]))
        obj = {'Id': pin()['image_id'], 'RepoDigests': [pin()['image']],
               'Config': {'Labels': {'org.opencontainers.image.revision': 'a' * 40}}}
        stack.enter_context(patch.object(d, 'command', return_value=json.dumps([obj])))
        stack.enter_context(patch.object(d, 'load', return_value={'accepted': []}))
        return stack.enter_context(patch.multiple(d, inspect_image=DEFAULT, verify_provenance=DEFAULT,
                    accepted_compatibility=DEFAULT, image_capability=DEFAULT, atomic=DEFAULT))

    def test_stage_requires_verified_discovery_before_any_capability_execution(self):
        for failure in ('verify_provenance', 'accepted_compatibility', 'discovery'):
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                mocks = self.stage_context(stack)
                discovery = stack.enter_context(patch.object(core.DevelopmentSource, 'check_discovery'))
                (discovery if failure == 'discovery' else mocks[failure]).side_effect = core.Rejected(failure)
                with self.assertRaises(core.Rejected):
                    d.stage()
                mocks['image_capability'].assert_not_called()
                mocks['atomic'].assert_not_called()

    def test_stage_writes_explicit_development_pin(self):
        with contextlib.ExitStack() as stack:
            mocks = self.stage_context(stack)
            stack.enter_context(patch.object(core.DevelopmentSource, 'check_discovery'))
            self.assertEqual(d.stage(), pin())
            mocks['atomic'].assert_called_once_with(d.ROOT / 'staged.json', pin())

    def test_channel_label_checked_before_execution(self):
        labels = {'org.opencontainers.image.revision': 'a' * 40, 'org.opencontainers.image.version': '0.0.0',
                  'org.opencontainers.image.source': 'https://github.com/atrinik/classic'}
        obj = {'Id': pin()['image_id'], 'Architecture': 'amd64', 'Os': 'linux', 'Config': {'Labels': labels}}
        with settings(), patch.object(d, 'command', return_value=json.dumps([obj])), self.assertRaisesRegex(core.Rejected, 'channel'):
            d.inspect_image(pin())
        labels['org.atrinik.release-channel'] = 'development'
        with settings(), patch.object(d, 'command', return_value=json.dumps([obj])):
            d.inspect_image(pin())

    def test_activation_without_compatibility_acceptance_cannot_drain(self):
        def load(path):
            return {'activation_enabled': True} if path.name == 'policy.json' else pin('e') if path.name == 'pin.json' else {'accepted': []}
        with settings(), patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.multiple(d, fence=DEFAULT, check_monotonic=DEFAULT, inspect_image=DEFAULT, verify_provenance=DEFAULT, image_capability=DEFAULT, admin_stop=DEFAULT, atomic=DEFAULT) as mocks, patch.object(d, 'accepted_compatibility', side_effect=core.Rejected('missing acceptance')):
            with self.assertRaisesRegex(core.Rejected, 'acceptance'):
                d.activate(pin())
            mocks['admin_stop'].assert_not_called()
            mocks['atomic'].assert_not_called()

    def test_run_retains_pin_without_discovery(self):
        with tempfile.TemporaryDirectory() as tmp, settings(), patch.object(d, 'LOCK', Path(tmp) / 'lock'), patch.object(d, 'load', return_value=pin()), patch.object(Path, 'exists', return_value=False), patch.multiple(d, fence=DEFAULT, verify_provenance=DEFAULT, image_capability=DEFAULT, accepted_compatibility=DEFAULT, guard=DEFAULT, launch=DEFAULT, healthy=DEFAULT, require_admin=DEFAULT, validate_access=DEFAULT, identity=DEFAULT), patch.object(d, 'command', side_effect=['', '0']), patch.object(core.DevelopmentSource, 'manifest') as manifest:
            self.assertEqual(d.run(), 0)
            manifest.assert_not_called()


if __name__ == '__main__':
    unittest.main()
