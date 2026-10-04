import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import core
from adapters import classic


class FakeAdapter:
    """Synthetic service with durable records and a controllable lifecycle."""
    SERVICE = 'fixture.service'
    CONTAINER = 'fixture'
    atomic = staticmethod(core.atomic)
    def __init__(self, root, fail=None):
        self.ROOT = root
        self.fail = fail
        self.events = []
        for name, value in {'policy': {'activation_enabled': True}, 'pin': {'version': '1.0.0'}, 'ledger': {'accepted': [{'version': '1.0.0'}]}}.items():
            core.atomic(root / (name + '.json'), value)
    def load(self, path):
        return json.loads(path.read_text())
    def event(self, name):
        self.events.append(name)
        if self.fail == name:
            raise core.Rejected('injected ' + name)
    def __getattr__(self, name):
        if name not in {'fence', 'check_monotonic', 'inspect_image', 'verify_provenance', 'capability', 'compatibility', 'headroom', 'drain', 'stopped', 'clone_check', 'launch', 'healthy', 'require_control', 'validate_access', 'validate_runtime', 'stop_isolated', 'restore', 'retain'}:
            raise AttributeError(name)
        return lambda *args: self.event(name)
    def command(self, *args, **kwargs):
        if args[:2] == ('docker', 'inspect'):
            return '[{}]'
        self.event(' '.join(args[:2]))
        return ''
    def guard(self, mode):
        phase = self.load(self.ROOT / 'transaction.json')['phase']
        if mode == 'public' and phase != 'may-have-played':
            raise AssertionError('opened before durable boundary')
        self.event('guard-' + mode)
    def identity(self):
        return 'fixture-identity'
    def archive(self):
        self.event('archive')
        backup = self.ROOT / 'backup'
        backup.mkdir()
        core.atomic(backup / 'manifest.json', {'identity': self.identity()})
        return backup


class Transaction(unittest.TestCase):
    def run_case(self, failure=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter(root, failure)
            with (root / 'lock').open('a') as lock:
                core.fcntl.flock(lock, core.fcntl.LOCK_EX)
                if failure:
                    with self.assertRaises(core.Rejected):
                        core.activate(adapter, {'version': '2.0.0'}, lock)
                else:
                    core.activate(adapter, {'version': '2.0.0'}, lock)
            phase = adapter.load(root / 'transaction.json')['phase'] if (root / 'transaction.json').exists() else None
            return adapter.events, phase
    def test_complete_transaction_orders_drain_backup_clone_and_public_boundary(self):
        events, phase = self.run_case()
        self.assertIsNone(phase)
        for earlier, later in [('verify_provenance', 'drain'), ('drain', 'archive'), ('archive', 'clone_check'), ('clone_check', 'launch'), ('healthy', 'guard-public')]:
            self.assertLess(events.index(earlier), events.index(later))
        self.assertNotIn('restore', events)
    def test_failed_drain_keeps_running_service_and_does_not_archive(self):
        events, phase = self.run_case('drain')
        self.assertEqual(phase, 'draining')
        for event in ('guard-closed', 'archive', 'restore', 'launch'):
            self.assertNotIn(event, events)
    def test_failed_clone_retains_transaction_without_launch(self):
        events, phase = self.run_case('clone_check')
        self.assertEqual(phase, 'testing')
        self.assertNotIn('launch', events)
        self.assertNotIn('guard-public', events)
    def test_failed_closed_activation_restores_complete_cohort_and_stays_stopped(self):
        events, phase = self.run_case('healthy')
        self.assertEqual(phase, 'rollback-review')
        self.assertIn('restore', events)
        self.assertNotIn('guard-public', events)
    def test_uncertain_public_activation_never_restores_stale_state(self):
        events, phase = self.run_case('guard-public')
        self.assertEqual(phase, 'may-have-played')
        self.assertNotIn('restore', events)
    def test_default_disabled_and_nonboolean_policy_cannot_activate(self):
        for policy in ({}, {'activation_enabled': False}, {'activation_enabled': 'true'}, {'activation_enabled': 1}):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter = FakeAdapter(root)
                core.atomic(root / 'policy.json', policy)
                with self.assertRaises(core.Rejected):
                    core.activate(adapter, {'version': '2.0.0'})
                self.assertNotIn('drain', adapter.events)


class PublicSource(unittest.TestCase):
    def source(self):
        return core.ReleaseSource('atrinik/example-service', 'ghcr.io/atrinik/example-service', 'atrinik/example-service/.github/workflows/release.yml')
    def test_source_supports_distinct_service_without_classic_assumptions(self):
        with patch.object(core, 'public_bytes', return_value=b'{"id": 123}') as request:
            self.assertEqual(self.source().api('/releases/latest'), {'id': 123})
            self.assertEqual(request.call_args.args[0], 'https://api.github.com/repos/atrinik/example-service/releases/latest')
            self.assertNotIn('Authorization', request.call_args.args[1])
    def test_exact_manifest_bytes_not_json_reserialization_define_digest(self):
        raw = b'{ "schemaVersion": 2, "manifests": [] }\n'
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        with patch.object(core, 'public_bytes', side_effect=[b'{"token":"public-fixture"}', raw]):
            self.assertEqual(self.source().manifest(digest), (digest, raw))
        with patch.object(core, 'public_bytes', side_effect=[b'{"token":"public-fixture"}', raw+b' ']), self.assertRaisesRegex(core.Rejected, 'digest mismatch'):
            self.source().manifest(digest)
    def test_redirects_rejected_before_following_destination(self):
        with self.assertRaises(core.Rejected):
            core.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://attacker.invalid/')
    def test_ambiguous_platform_rejected(self):
        child = {'digest': 'sha256:'+'a'*64, 'platform': {'os':'linux','architecture':'amd64'}}
        self.assertEqual(core.amd64_child(json.dumps({'schemaVersion':2,'manifests':[child]})), child['digest'])
        with self.assertRaises(core.Rejected):
            core.amd64_child(json.dumps({'schemaVersion':2,'manifests':[child,child]}))
    def test_verifier_has_only_public_readonly_mount_and_no_credentials_or_network(self):
        args = core.verifier_args(Path('/tmp/public-evidence'), 'a'*40, 'ghcr.io/atrinik/service-updater@sha256:'+'b'*64, 'atrinik/example-service', 'atrinik/example-service/.github/workflows/release.yml')
        self.assertEqual(args[args.index('--network')+1], 'none')
        self.assertEqual(args.count('--mount'), 1)
        self.assertEqual(args[args.index('--mount')+1], 'type=bind,src=/tmp/public-evidence,dst=/evidence,readonly')
        for forbidden in ('--privileged','--pid','--net'):
            self.assertNotIn(forbidden, args)
        for forbidden in ('docker.sock','GH_TOKEN','GITHUB_TOKEN'):
            self.assertFalse(any(forbidden in arg for arg in args))
        for required in ('--bundle','--custom-trusted-root','--source-digest','--signer-workflow','--read-only','--cap-drop'):
            self.assertIn(required,args)
    def test_unpinned_verifier_and_empty_verification_rejected(self):
        with self.assertRaises(core.Rejected):
            core.verifier_args(Path('/tmp/evidence'),'a'*40,'ghcr.io/atrinik/service-updater:latest','atrinik/example-service','atrinik/example-service/.github/workflows/release.yml')
        with self.assertRaises(core.Rejected):
            core.verify_index(b'{}','a'*40,[{'bundle':{}}],lambda *args:['fixture'],lambda *args,**kw:'[]')


class ImageIdentity(unittest.TestCase):
    def pin(self):
        return {'version':'0.1.0','image':'ghcr.io/atrinik/classic-server@sha256:'+'a'*64,
                'index_image':'ghcr.io/atrinik/classic-server@sha256:'+'b'*64,
                'config_digest':'sha256:'+'c'*64,'image_id':'sha256:'+'c'*64,
                'revision':'d'*40,'release_id':1}
    def test_classic_and_containerd_store_ids_bound_to_attested_graph(self):
        for digest in ('a','b','c'):
            pin = dict(self.pin(),image_id='sha256:'+digest*64)
            classic.validate_pin(pin)
        with self.assertRaisesRegex(core.Rejected,'attested graph'):
            classic.validate_pin(dict(self.pin(),image_id='sha256:'+'e'*64))
    def test_child_config_binding_cannot_be_replaced_by_engine_index_identity(self):
        pin=dict(self.pin(),image_id='sha256:'+'b'*64)
        index=json.dumps({'schemaVersion':2,'manifests':[{'digest':'sha256:'+'a'*64,'platform':{'os':'linux','architecture':'amd64'}}]}).encode()
        child=json.dumps({'config':{'digest':'sha256:'+'c'*64}}).encode()
        with patch.object(classic,'manifest',side_effect=[('index',index),('child',child)]), patch.object(classic,'verify_index'):
            classic.verify_provenance(pin)
        child=json.dumps({'config':{'digest':'sha256:'+'e'*64}}).encode()
        with patch.object(classic,'manifest',side_effect=[('index',index),('child',child)]), patch.object(classic,'verify_index'), self.assertRaisesRegex(core.Rejected,'config digest disagree'):
            classic.verify_provenance(pin)
    def test_all_server_runs_select_amd64(self):
        args=classic.container_args('test',Path('/fixture'),self.pin(),True)
        self.assertEqual(args[args.index('--platform')+1],'linux/amd64')
        with patch.object(classic,'command',return_value='admin_shutdown_socket') as run:
            classic.image_capability(self.pin())
            args=run.call_args.args
            self.assertEqual(args[args.index('--platform')+1],'linux/amd64')
    def test_actual_container_image_must_match_engine_identity(self):
        pin=self.pin()
        runtime={'Image':pin['image_id'],'Config':{'Image':pin['image_id'],'User':'10001:10001','Labels':{'org.atrinik.development.managed':'true'}},'HostConfig':{'NetworkMode':'host'},'Mounts':[
            {'Type':'bind','Destination':'/opt/atrinik/server/data','Source':str(classic.STATE/'server-data'),'RW':True},
            {'Type':'bind','Destination':'/opt/atrinik/server/server-custom.cfg','Source':str(classic.STATE/'config/server-custom.cfg'),'RW':False},
            {'Type':'bind','Destination':str(classic.ADMIN.parent),'Source':str(classic.ADMIN.parent),'RW':True}]}
        classic.validate_runtime(runtime,pin)
        runtime['Image']='sha256:'+'e'*64
        with self.assertRaises(core.Rejected):classic.validate_runtime(runtime,pin)
    def test_stable_major_zero_versions_supported(self):
        self.assertEqual(core.version('0.0.1'),(0,0,1))
        self.assertEqual(core.version('0.1.0'),(0,1,0))


class Configuration(unittest.TestCase):
    def fixture(self):
        data = json.loads((Path(__file__).parents[1] / 'config/deployment.example.json').read_text())
        data.update(machine_id='a'*32, state_filesystem_uuid='11111111-1111-4111-8111-111111111111', production_identity_sha256='b'*64, updater_image='ghcr.io/atrinik/service-updater@sha256:'+'c'*64)
        return data
    def test_all_fences_mandatory_and_unknown_fields_rejected(self):
        data = self.fixture()
        classic.validate_deployment(data)
        for key in data:
            bad = dict(data)
            del bad[key]
            with self.subTest(key=key), self.assertRaises(core.Rejected):
                classic.validate_deployment(bad)
        with self.assertRaises(core.Rejected):
            classic.validate_deployment(dict(data, skip_fences=True))
    def test_overlapping_paths_identity_and_unpublished_sources_rejected(self):
        data = self.fixture()
        for key,value in [('root',data['state']),('backups',data['state_mount']+'/backups'),('production_service',data['service']),('production_container',data['container']),('root','/opt/../etc'),('adapter','shell'),('release_repository','attacker/classic'),('release_image','ghcr.io/attacker/classic'),('updater_image','ghcr.io/atrinik/service-updater:latest')]:
            with self.subTest(key=key), self.assertRaises(core.Rejected):
                classic.validate_deployment(dict(data, **{key:value}))
    def test_checked_schema_matches_authoritative_validation_fields(self):
        schema=json.loads((Path(__file__).parents[1]/'config/deployment.schema.json').read_text())
        self.assertEqual(set(schema['required']),set(classic.CONFIG_FIELDS)|{'schema_version'})
        self.assertFalse(schema['additionalProperties'])


if __name__ == '__main__':
    unittest.main()
