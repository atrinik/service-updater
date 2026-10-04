"""Reject config forms whose INI and native CLI meanings can diverge."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adapters import classic as d


class NativeConfiguration(unittest.TestCase):
    def parse(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server-custom.cfg'
            path.write_bytes(data.encode() if isinstance(data, str) else data)
            return d.validate_config(path)

    def test_native_standalone_assignments_and_optional_paths_remain_supported(self):
        text = '# Configured values\n[general]\nserver_name = "Synthetic Server"\n[meta]\naccess_required = true\naccess_initialize = false\naccess_store = /opt/atrinik/server/data/access-tokens\nserver_desc = A synthetic server\n'
        for raw in (text, text.replace('\n', '\r\n'), text.rstrip('\n'), text.replace(' = ', '\t=\t')):
            self.assertEqual(self.parse(raw), {'policy': 'protected'})
        self.assertEqual(self.parse('[meta]\naccess_required=false\n'), {'policy': 'open'})

    def test_obsolete_account_option_is_rejected_in_every_section(self):
        for section in ('meta', 'general'):
            for value in ('/opt/atrinik/server/access-admin-accounts', '/external', 'fixture'):
                with self.subTest(section=section, value=value), self.assertRaisesRegex(d.Rejected, 'obsolete'):
                    self.parse('[meta]\naccess_required=true\n' +
                               ('[general]\n' if section == 'general' else '') +
                               'access_admin_accounts=' + value + '\n')

    def test_indented_native_override_is_not_an_ini_continuation(self):
        for whitespace in (' ', '\t', '    '):
            text = '[meta]\naccess_required=true\nserver_desc=Fixture\n' + whitespace + 'access_required=false\n'
            with self.subTest(whitespace=repr(whitespace)), self.assertRaisesRegex(d.Rejected, 'indented'):
                self.parse(text)

    def test_uppercase_and_colon_syntax_cannot_fabricate_native_policy(self):
        for line in ('ACCESS_REQUIRED=true', 'Access_Required=true', 'access_required: true'):
            with self.subTest(line=line), self.assertRaises(d.Rejected):
                self.parse('[meta]\n' + line + '\n')

    def test_native_global_options_cannot_hide_in_other_sections(self):
        for section in ('general', 'other', 'DEFAULT', 'META'):
            with self.subTest(section=section), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=true\n[' + section + ']\naccess_required=false\n')
        with self.assertRaises(d.Rejected):
            self.parse('[general]\naccess_required=true\n')

    def test_duplicate_options_are_rejected_across_all_sections(self):
        for second in ('[meta]\naccess_required=false', '[general]\nserver_name=second'):
            with self.subTest(second=second), self.assertRaisesRegex(d.Rejected, 'duplicate'):
                self.parse('[general]\nserver_name=first\n[meta]\naccess_required=true\n' + second + '\n')

    def test_recursive_config_and_every_controlled_prefix_alias_are_rejected(self):
        controlled = ('config', 'access_required', 'access_initialize', 'access_store',
                      'access_admin_accounts', 'join_password', 'join_password_file',
                      'rendezvous_invite_file', 'metaserver_hostname', 'server_desc')
        aliases = {name[:length] for name in controlled for length in range(1, len(name))}
        for name in sorted(aliases | {'config'}):
            with self.subTest(name=name), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=true\n' + name + '=/fixture/override\n')
        for section in ('meta', 'general'):
            with self.subTest(section=section), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=true\n[' + section + ']\nconfig=/fixture/override\n')

    def test_literal_quotes_are_not_stripped_from_native_controlled_values(self):
        for value in ('"true"', "'true'", '"true', 'true"', 'false # note', 'true;note'):
            with self.subTest(value=value), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=' + value + '\n')
        for key, value in (('access_store', '"/opt/atrinik/server/data/access-tokens"'),
                           ('access_admin_accounts', '"/opt/atrinik/server/access-admin-accounts"')):
            with self.subTest(key=key), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=true\n' + key + '=' + value + '\n')

    def test_file_indirection_escapes_control_characters_and_blank_arguments_rejected(self):
        for value in ('<fixture', 'line\\naccess_required=false', 'value\x00tail', 'value\rtail', 'value\x7ftail', ''):
            with self.subTest(value=repr(value)), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=true\nserver_name=' + value + '\n')
        for data in (b'\xef\xbb\xbf[meta]\naccess_required=true\n', b'[meta]\naccess_required=true\r',
                     b'[meta]\naccess_required=true\n\xff=x\n'):
            with self.subTest(data=data[:30]), self.assertRaises(d.Rejected):
                self.parse(data)

    def test_unsupported_comments_or_section_suffixes_are_not_ini_metadata(self):
        for line in ('; comment', ' # comment', '[meta] # comment', '[DEFAULT]', '[meta]:', '[with-hyphen]', '[meta]', '# escape\\text', '# <file'):
            with self.subTest(line=line), self.assertRaises(d.Rejected):
                self.parse('[meta]\naccess_required=true\n' + line + '\n')
        self.assertEqual(self.parse('# safe comment\n \t\n[meta]\naccess_required=true\n')['policy'], 'protected')
        with self.assertRaises(d.Rejected):
            self.parse('server_name=unsectioned\n[meta]\naccess_required=true\n')

    def test_physical_comment_and_snprintf_assignment_limits_are_byte_based(self):
        base = '[meta]\naccess_required=true\n'
        self.assertEqual(self.parse(base + '#' + 'x' * 4093 + '\n')['policy'], 'protected')
        for line in ('#' + 'x' * 4094, 'server_name=' + 'x' * (4094 - len('server_name=')),
                     'server_name=' + 'é' * 2042):
            with self.subTest(size=len(line.encode())), self.assertRaises(d.Rejected):
                self.parse(base + line + '\n')
        self.assertEqual(self.parse(base + 'server_name=' + 'x' * (4093 - len('server_name=')) + '\n')['policy'], 'protected')

    def test_invalid_config_fails_before_status_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'config').mkdir()
            (state / 'config/server-custom.cfg').write_text('[meta]\naccess_required=true\nserver_desc=Fixture\n access_required=false\n')
            with patch.object(d, 'command') as command, patch.object(d, 'offline_access_status') as inspect, self.assertRaises(d.Rejected):
                d.validate_access('a' * 64, state=state, pin={})
            command.assert_not_called()
            inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
