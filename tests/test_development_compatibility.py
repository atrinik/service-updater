"""Explicit source-only development admission never claims map gameplay proof."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import DEFAULT, patch

from adapters import classic as d

FIX = 'a' * 40
NEXT = 'b' * 40


def policy(**changes):
    return dict({'compatibility_policy': 'published-source', 'source_revision': FIX}, **changes)


def pin(revision=FIX, version='0.0.0'):
    return {'revision': revision, 'version': version}


class DevelopmentCompatibility(unittest.TestCase):
    def test_same_source_needs_no_fabricated_roundtrip_or_compare(self):
        with patch.object(d, 'RELEASE_CHANNEL', 'development'), patch.object(d, 'load', return_value=policy()), patch.object(d, 'api') as api:
            d.accepted_compatibility(pin())
            api.assert_not_called()

    def test_descendant_must_include_exact_selected_fix(self):
        with patch.object(d, 'RELEASE_CHANNEL', 'development'), patch.object(d, 'load', return_value=policy()), patch.object(d, 'api', return_value={'status': 'ahead', 'merge_base_commit': {'sha': FIX}}) as api:
            d.accepted_compatibility(pin(NEXT))
            api.assert_called_once_with('/compare/' + FIX + '...' + NEXT)

    def test_old_divergent_or_wrong_merge_base_source_rejected(self):
        for comparison in ({'status': 'behind'}, {'status': 'diverged'}, {'status': 'identical'},
                           {'status': 'ahead'}, {'status': 'ahead', 'merge_base_commit': {'sha': NEXT}}):
            with self.subTest(comparison=comparison), patch.object(d, 'RELEASE_CHANNEL', 'development'), patch.object(d, 'load', return_value=policy()), patch.object(d, 'api', return_value=comparison), self.assertRaises(d.Rejected):
                d.accepted_compatibility(pin(NEXT))

    def test_stable_rejects_new_policy_even_with_roundtrip_fields(self):
        for acceptance in (policy(), policy(private_map_roundtrip=True, private_map_count=5, minimum_version='5.80.0')):
            with self.subTest(acceptance=acceptance), patch.object(d, 'RELEASE_CHANNEL', 'stable'), patch.object(d, 'load', return_value=acceptance), patch.object(d, 'api') as api, self.assertRaises(d.Rejected):
                d.accepted_compatibility(pin())
            api.assert_not_called()

    def test_published_source_policy_has_exact_fields_and_valid_revision(self):
        bad = [None, [], {}, {'compatibility_policy': 'published-source'},
               policy(private_map_roundtrip=True), policy(private_map_count=0), policy(minimum_version='0.0.0')]
        bad += [policy(compatibility_policy=value) for value in (None, '', 'roundtrip', True)]
        bad += [policy(source_revision=value) for value in (None, True, '', 'A' * 40, 'a' * 39, 'a' * 41)]
        for acceptance in bad:
            with self.subTest(acceptance=acceptance), patch.object(d, 'RELEASE_CHANNEL', 'development'), patch.object(d, 'load', return_value=acceptance), patch.object(d, 'api') as api, self.assertRaises(d.Rejected):
                d.accepted_compatibility(pin())
            api.assert_not_called()

    def test_original_policy_keeps_actual_roundtrip_and_stable_minimum(self):
        acceptance = {'private_map_roundtrip': True, 'private_map_count': 5,
                      'minimum_version': '5.80.0', 'source_revision': FIX}
        for channel in ('stable', 'development'):
            with self.subTest(channel=channel), patch.object(d, 'RELEASE_CHANNEL', channel), patch.object(d, 'load', return_value=acceptance):
                d.accepted_compatibility(pin(version='5.80.0'))
            for changes in ({'private_map_roundtrip': False}, {'private_map_count': 4}, {'private_map_count': True}):
                with self.subTest(channel=channel, changes=changes), patch.object(d, 'RELEASE_CHANNEL', channel), patch.object(d, 'load', return_value=dict(acceptance, **changes)), self.assertRaises(d.Rejected):
                    d.accepted_compatibility(pin(version='5.80.0'))
        with patch.object(d, 'RELEASE_CHANNEL', 'stable'), patch.object(d, 'load', return_value=acceptance), self.assertRaises(d.Rejected):
            d.accepted_compatibility(pin(version='5.79.0'))

    def test_retained_run_admits_explicit_development_policy_before_launch(self):
        def load(path):
            return policy() if path.name == 'acceptance.json' else pin()
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'RELEASE_CHANNEL', 'development'), patch.object(d, 'LOCK', Path(temp) / 'lock'), patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.multiple(d, fence=DEFAULT, verify_provenance=DEFAULT, image_capability=DEFAULT, guard=DEFAULT, launch=DEFAULT, healthy=DEFAULT, require_admin=DEFAULT, validate_access=DEFAULT, identity=DEFAULT) as mocks, patch.object(d, 'command', side_effect=['', '0']):
            self.assertEqual(d.run(), 0)
            mocks['verify_provenance'].assert_called_once()
            mocks['launch'].assert_called_once()
            mocks['require_admin'].assert_called_once()
            mocks['validate_access'].assert_called_once()

    def test_invalid_source_policy_run_never_launches_or_opens_guard(self):
        def load(path):
            return policy(source_revision='invalid') if path.name == 'acceptance.json' else pin()
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'RELEASE_CHANNEL', 'development'), patch.object(d, 'LOCK', Path(temp) / 'lock'), patch.object(d, 'load', side_effect=load), patch.object(Path, 'exists', return_value=False), patch.multiple(d, fence=DEFAULT, verify_provenance=DEFAULT, image_capability=DEFAULT, guard=DEFAULT, launch=DEFAULT) as mocks, self.assertRaises(d.Rejected):
            d.run()
        mocks['launch'].assert_not_called()
        mocks['guard'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
