"""Native capability negotiation, transport bounds and save-gate regressions."""
import json
from pathlib import Path
import stat
import struct
import unittest
from unittest.mock import patch

from adapters import classic as d

PREFIX = 'ATRINIK-ADMIN/1 CAPABILITIES '
REQUIRED = 'shutdown-v1 durable-result-v1'


class Capabilities(unittest.TestCase):
    def test_reordered_unknown_capabilities_are_accepted(self):
        response = PREFIX + 'access-tokens-v1 durable-result-v1 future-2 shutdown-v1'
        self.assertEqual(d.admin_capabilities(response),
                         frozenset(('access-tokens-v1', 'durable-result-v1', 'future-2', 'shutdown-v1')))
        with patch.object(d, 'command', return_value='[{"State":{"Pid":123}}]'), patch.object(d, 'admin_request', return_value=response) as request:
            d.require_admin('fixture')
        request.assert_called_once_with('ATRINIK-ADMIN/1 CAPABILITIES', 123)

    def test_token_count_and_length_boundaries(self):
        tokens = REQUIRED.split() + ['future-' + str(i) for i in range(29)] + ['x' * 48]
        self.assertEqual(len(d.admin_capabilities(PREFIX + ' '.join(tokens))), 32)
        for bad in (tokens + ['overflow'], tokens[:-1] + ['x' * 49]):
            with self.subTest(tokens=len(bad)), self.assertRaises(d.Rejected):
                d.admin_capabilities(PREFIX + ' '.join(bad))

    def test_malformed_duplicate_missing_or_oversized_sets_are_rejected(self):
        invalid = [None, b'not-text', '', PREFIX, PREFIX + 'shutdown-v1',
                   PREFIX + 'durable-result-v1', PREFIX + 'future-only',
                   PREFIX + REQUIRED + ' shutdown-v1', PREFIX + REQUIRED + ' x x',
                   PREFIX + REQUIRED + ' Upper', PREFIX + REQUIRED + ' under_score',
                   PREFIX + REQUIRED + ' nonascii-\u00e9', PREFIX + REQUIRED + '\x00',
                   PREFIX + REQUIRED + '\n', PREFIX + REQUIRED + '\r',
                   PREFIX + REQUIRED + ' ', PREFIX + ' ' + REQUIRED,
                   PREFIX + REQUIRED.replace(' ', '\t'), PREFIX + REQUIRED.replace(' ', '  '),
                   'ATRINIK-ADMIN/2 CAPABILITIES ' + REQUIRED,
                   PREFIX + REQUIRED + ' ' + 'x' * 2048]
        for response in invalid:
            with self.subTest(response=repr(response)[:90]), self.assertRaises(d.Rejected):
                d.admin_capabilities(response)

    def wire(self, chunks):
        with patch.object(d, 'private'), patch.object(Path, 'lstat') as info, patch.object(d.socket, 'socket') as factory:
            info.return_value.st_mode = stat.S_IFSOCK | 0o600
            info.return_value.st_uid = 10001
            client = factory.return_value.__enter__.return_value
            client.getsockopt.return_value = struct.pack('3i', 123, 10001, 10001)
            client.recv.side_effect = chunks
            return d.admin_request('ATRINIK-ADMIN/1 CAPABILITIES', 123)

    def test_fragmented_capability_line_above_old_limit_is_read_exactly(self):
        tokens = REQUIRED.split() + ['future-' + str(i) + '-' + 'x' * 35 for i in range(30)]
        wire = (PREFIX + ' '.join(tokens) + '\n').encode()
        self.assertGreater(len(wire), 1024)
        self.assertLessEqual(len(wire), 2048)
        response = self.wire([wire[:700], wire[700:], b''])
        self.assertEqual(len(d.admin_capabilities(response)), 32)

    def test_transport_rejects_trailing_frames_nonascii_overflow_and_missing_lf(self):
        valid = (PREFIX + REQUIRED + '\n').encode()
        cases = [[valid, b'garbage', b''], [valid, valid, b''],
                 [valid[:-1], b''], [b'\xff\n', b''], [b'x' * 2049]]
        for chunks in cases:
            with self.subTest(chunks=[len(c) for c in chunks]), self.assertRaises(d.Rejected):
                self.wire(chunks)
        # Preserve whitespace so the grammar rejects it instead of strip() hiding it.
        response = self.wire([(PREFIX + REQUIRED + ' \n').encode(), b''])
        with self.assertRaises(d.Rejected):
            d.admin_capabilities(response)

    def stopped_fixture(self, capabilities, receipt='saved'):
        request_id = 'a' * 32
        running = {'Id': 'container-id', 'State': {'Pid': 123, 'Running': True}}
        stopped = {'Id': 'container-id', 'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False}}
        commands = ['container-id', json.dumps([running]), json.dumps([stopped])]
        with patch.object(d, 'command', side_effect=commands), patch.object(d, 'admin_request', side_effect=[capabilities, 'ATRINIK-ADMIN/1 SCHEDULED ' + request_id]) as request, patch.object(d.secrets, 'token_hex', return_value=request_id), patch.object(Path, 'exists', return_value=False), patch.object(d, 'private'), patch.object(Path, 'read_text', return_value='ATRINIK-ADMIN/1 RESULT ' + request_id + ' ' + receipt + '\n'):
            try:
                d.admin_stop('fixture', Path('/fixture/admin.sock'))
            finally:
                self.requests = [call.args[0] for call in request.call_args_list]

    def test_extended_set_still_requires_exact_native_saved_receipt(self):
        response = PREFIX + REQUIRED + ' access-tokens-v1'
        self.stopped_fixture(response)
        self.assertEqual(len(self.requests), 2)
        self.assertIn(' 60 ', self.requests[1])
        with self.assertRaises(d.Rejected):
            self.stopped_fixture(response, 'failed')

    def test_invalid_capability_set_never_schedules_shutdown(self):
        for response in (PREFIX + 'access-tokens-v1', PREFIX + REQUIRED + ' shutdown-v1'):
            with self.subTest(response=response), self.assertRaises(d.Rejected):
                self.stopped_fixture(response)
            self.assertEqual(self.requests, ['ATRINIK-ADMIN/1 CAPABILITIES'])


if __name__ == '__main__':
    unittest.main()
