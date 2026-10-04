import json
from pathlib import Path
import unittest
from unittest.mock import patch

from adapters import classic as d


class LoadedConditions(unittest.TestCase):
    UNIT = 'fixture-runtime.service'
    PATH = '/org/freedesktop/systemd1/unit/fixture_2druntime_2eservice'
    MARKER = Path('/etc/fixture-migrated')

    def responses(self, conditions=None):
        if conditions is None:
            conditions = [['ConditionPathExists', False, True, str(self.MARKER), 0]]
        return [{'type': 'o', 'data': [self.PATH]},
                {'type': 's', 'data': self.UNIT},
                {'type': 'a(sbbsi)', 'data': conditions}]

    def verify(self, responses):
        values = [value if isinstance(value, str) else json.dumps(value) for value in responses]
        with patch.object(d, 'PRODUCTION_SERVICE', self.UNIT), patch.object(d, 'PRODUCTION_MARKER', self.MARKER), patch.object(d, 'command', side_effect=values) as commands:
            d.loaded_production_fence()
        return commands.call_args_list

    def test_typed_loaded_condition_works_without_human_readable_systemctl(self):
        calls = self.verify(self.responses())
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].args, ('busctl', '--system', '--json=short', 'call',
            'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
            'org.freedesktop.systemd1.Manager', 'GetUnit', 's', self.UNIT))
        for call, prop in zip(calls[1:], ('Id', 'Conditions')):
            self.assertEqual(call.args, ('busctl', '--system', '--json=short', 'get-property',
                'org.freedesktop.systemd1', self.PATH, 'org.freedesktop.systemd1.Unit', prop))

    def test_unrelated_real_systemd_condition_shapes_remain_compatible(self):
        # Captured standard systemd-pstore tuple shapes, with no host identity.
        unrelated = [['ConditionVirtualization', False, True, 'container', 0],
                     ['ConditionDirectoryNotEmpty', False, False, '/sys/fs/pstore', 0]]
        with self.assertRaisesRegex(d.Rejected, 'not loaded'):
            self.verify(self.responses(unrelated))
        self.verify(self.responses(unrelated + self.responses()[2]['data']))

    def test_last_evaluation_result_does_not_replace_configured_fence(self):
        for state in (0, -1, 1, -(2 ** 31), 2 ** 31 - 1):
            with self.subTest(state=state):
                self.verify(self.responses([['ConditionPathExists', False, True, str(self.MARKER), state]]))

    def test_absent_trigger_unnegated_wrong_name_or_partial_path_rejected(self):
        fixtures = [[], [['ConditionPathExists', True, True, str(self.MARKER), 0]],
                    [['ConditionPathExists', False, False, str(self.MARKER), 0]],
                    [['ConditionDirectoryNotEmpty', False, True, str(self.MARKER), 0]],
                    [['ConditionPathExists', False, True, str(self.MARKER) + '-other', 0]],
                    [['ConditionPathExists', False, True, '/elsewhere', 0],
                     ['ConditionDirectoryNotEmpty', False, True, str(self.MARKER), 0]]]
        for conditions in fixtures:
            with self.subTest(conditions=conditions), self.assertRaisesRegex(d.Rejected, 'not loaded'):
                self.verify(self.responses(conditions))

    def test_wrong_signature_and_malformed_top_level_rejected(self):
        for value in ('Conditions=[unprintable]', '{}', '[]', 'null',
                      {'type': 's', 'data': self.responses()[2]['data']},
                      {'type': 'a(sbbsi)', 'data': {}, 'extra': True},
                      {'type': 'a(sbbsi)', 'data': None}):
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                self.verify(self.responses()[:2] + [value])

    def test_tuple_fields_require_exact_dbus_types_and_signed_32_bit_state(self):
        good = ['ConditionPathExists', False, True, str(self.MARKER), 0]
        bad = [None, {}, [], good[:-1], good + [0]]
        for position, value in [(0, None), (0, 1), (1, 0), (1, 'false'), (2, 1),
                                (2, 'true'), (3, None), (3, 1), (4, False),
                                (4, '0'), (4, 0.0), (4, -(2 ** 31) - 1), (4, 2 ** 31)]:
            condition = list(good)
            condition[position] = value
            bad.append(condition)
        for condition in bad:
            with self.subTest(condition=condition), self.assertRaises(d.Rejected):
                # A correct tuple cannot hide a malformed unrelated tuple.
                self.verify(self.responses([good, condition]))

    def test_getunit_path_and_canonical_identity_are_checked_before_conditions(self):
        for value in ({'type': 's', 'data': self.PATH}, {'type': 'o', 'data': self.PATH},
                      {'type': 'o', 'data': []}, {'type': 'o', 'data': [self.PATH, self.PATH]},
                      {'type': 'o', 'data': [123]}, {'type': 'o', 'data': ['/unrelated/object']},
                      {'type': 'o', 'data': [self.PATH + '/child']}):
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                self.verify([value])
        for value in ({'type': 's', 'data': 'different.service'},
                      {'type': 's', 'data': [self.UNIT]}, {'type': 'o', 'data': self.UNIT}):
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                self.verify(self.responses()[:1] + [value])

    def test_query_failure_has_no_text_or_disk_only_fallback(self):
        good = self.responses()
        for index in range(3):
            prefix = [json.dumps(value) for value in good[:index]]
            with self.subTest(index=index), patch.object(d, 'PRODUCTION_SERVICE', self.UNIT), patch.object(d, 'command', side_effect=prefix + [d.Rejected('query failed')]) as commands:
                with self.assertRaisesRegex(d.Rejected, 'query failed'):
                    d.loaded_production_fence()
                self.assertEqual(commands.call_count, index + 1)
        with patch.object(d, 'command', side_effect=FileNotFoundError), self.assertRaises(FileNotFoundError):
            d.loaded_production_fence()

    def test_disk_fence_alone_cannot_pass_host_fence_or_reach_docker(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if 'GetUnit' in args:
                return json.dumps({'type': 'o', 'data': [self.PATH]})
            if args[-1] == 'Id':
                return json.dumps({'type': 's', 'data': self.UNIT})
            return json.dumps({'type': 'a(sbbsi)', 'data': []})
        def read(path, *args, **kwargs):
            return 'machine-fixture' if str(path) == '/etc/machine-id' else 'ConditionPathExists=!' + str(self.MARKER)
        with patch.object(d, 'PRODUCTION_SERVICE', self.UNIT), patch.object(d, 'PRODUCTION_MARKER', self.MARKER), patch.object(d, 'MACHINE', 'machine-fixture'), patch.object(d.os, 'geteuid', return_value=0), patch.object(d, 'private'), patch.object(Path, 'read_text', autospec=True, side_effect=read), patch.object(d, 'command', side_effect=command):
            with self.assertRaisesRegex(d.Rejected, 'not loaded'):
                d.fence(False)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[0] == 'busctl' for call in calls))


if __name__ == '__main__':
    unittest.main()
