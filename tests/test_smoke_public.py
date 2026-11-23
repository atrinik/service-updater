import contextlib
import email.message
import importlib.util
import io
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch
import urllib.error
import core

SCRIPT = Path(__file__).parents[1] / 'scripts/smoke_public.py'
SPEC = importlib.util.spec_from_file_location('smoke_public', SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

URL = 'https://api.github.com/repos/atrinik/classic/releases/latest'
NOW = 1800000000


ERRORS = []


def http_error(code, headers=None):
    message = email.message.Message()
    for name, value in (headers or {}).items():
        message[name] = value
    error = urllib.error.HTTPError(URL, code, 'fixture', message, None)
    ERRORS.append(error)
    return error


def exhausted(reset):
    return http_error(403, {'X-RateLimit-Limit': '60', 'X-RateLimit-Remaining': '0',
                            'X-RateLimit-Reset': str(reset)})


def tearDownModule():
    for error in ERRORS:
        error.close()


class Fetch:
    """Replay outcomes for an anonymous fetch and record each request."""
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append((url, args, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RateLimitRetry(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def wrap(self, fetch, **kwargs):
        self.sleeps = []
        return smoke.retry_rate_limits(fetch, sleep=self.sleeps.append, clock=lambda: NOW, **kwargs)

    def test_rate_limited_403_waits_for_reset_then_succeeds(self):
        error = exhausted(NOW + 30)
        fetch = Fetch(error, b'{}')
        self.assertEqual(self.wrap(fetch)(URL, {'Accept': 'json'}, limit=5), b'{}')
        self.assertEqual(self.sleeps, [31])
        self.assertEqual(fetch.calls, [(URL, ({'Accept': 'json'},), {'limit': 5})] * 2)
        self.assertEqual(self.stderr.getvalue(), 'rate limited: ' + URL + ' returned HTTP 403; retrying in 31s\n')
        self.assertTrue(error.fp.closed)

    def test_429_honours_retry_after(self):
        fetch = Fetch(http_error(429, {'Retry-After': '7'}), b'{}')
        self.assertEqual(self.wrap(fetch)(URL), b'{}')
        self.assertEqual(self.sleeps, [7])

    def test_403_without_rate_limit_headers_fails_immediately(self):
        for headers in ({}, {'X-RateLimit-Remaining': '59', 'X-RateLimit-Reset': str(NOW + 30)}):
            with self.subTest(headers=headers):
                error = http_error(403, headers)
                fetch = Fetch(error, b'{}')
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.wrap(fetch)(URL)
                self.assertIs(caught.exception, error)
                self.assertEqual((len(fetch.calls), self.sleeps), (1, []))

    def test_other_http_errors_are_never_retried(self):
        hints = {'Retry-After': '1', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(NOW + 1)}
        for code, headers in ((400, hints), (401, hints), (404, hints), (422, hints), (429, {}),
                              (500, hints), (503, {'Retry-After': '1'})):
            with self.subTest(code=code, headers=headers):
                error = http_error(code, headers)
                fetch = Fetch(error, b'{}')
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.wrap(fetch)(URL)
                self.assertIs(caught.exception, error)
                self.assertEqual((len(fetch.calls), self.sleeps), (1, []))

    def test_rejections_and_network_errors_are_never_retried(self):
        for error in (core.Rejected('HTTP redirect rejected'), urllib.error.URLError('offline')):
            with self.subTest(error=error):
                fetch = Fetch(error, b'{}')
                with self.assertRaises(type(error)) as caught:
                    self.wrap(fetch)(URL)
                self.assertIs(caught.exception, error)
                self.assertEqual((len(fetch.calls), self.sleeps), (1, []))

    def test_wait_beyond_cap_fails_clearly_without_sleeping(self):
        fetch = Fetch(exhausted(NOW + 3600), b'{}')
        with self.assertRaisesRegex(smoke.RateLimited,
                                    r'^rate limited: .*/releases/latest returned HTTP 403 and asks for '
                                    r'3601s, more than the 300s left of the 300s retry budget$') as caught:
            self.wrap(fetch)(URL)
        self.assertIsInstance(caught.exception.__cause__, urllib.error.HTTPError)
        self.assertEqual((len(fetch.calls), self.sleeps), (1, []))

    def test_cap_is_shared_by_every_request(self):
        fetch = Fetch(http_error(429, {'Retry-After': '200'}), b'first',
                      http_error(429, {'Retry-After': '101'}), b'second')
        wrapped = self.wrap(fetch)
        self.assertEqual(wrapped(URL), b'first')
        with self.assertRaisesRegex(smoke.RateLimited, r'asks for 101s, more than the 100s left'):
            wrapped(URL)
        self.assertEqual(self.sleeps, [200])

    def test_wait_exactly_filling_the_cap_is_allowed(self):
        fetch = Fetch(http_error(429, {'Retry-After': '300'}), b'{}')
        self.assertEqual(self.wrap(fetch)(URL), b'{}')
        self.assertEqual(self.sleeps, [300])

    def test_retry_count_restarts_for_each_read(self):
        limited = [http_error(429, {'Retry-After': '1'}) for _ in range(6)]
        fetch = Fetch(*limited[:3], b'first', *limited[3:], b'second')
        wrapped = self.wrap(fetch)
        self.assertEqual((wrapped(URL), wrapped(URL)), (b'first', b'second'))
        self.assertEqual(self.sleeps, [1] * 6)

    def test_retries_per_request_are_bounded(self):
        fetch = Fetch(*[http_error(429, {'Retry-After': '1'}) for _ in range(4)], b'late')
        with self.assertRaisesRegex(smoke.RateLimited, r'still returned HTTP 429 after 3 retries$'):
            self.wrap(fetch)(URL)
        self.assertEqual((len(fetch.calls), self.sleeps), (4, [1, 1, 1]))

    def test_wait_hints(self):
        for headers, wait in (
            ({'Retry-After': '5', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(NOW + 100)}, 5),
            ({'Retry-After': ' 12 '}, 12),
            ({'Retry-After': '0'}, 1),
            ({'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}, 60),
            ({'X-RateLimit-Remaining': '0'}, 60),
            ({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': 'soon'}, 60),
            ({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(NOW - 10)}, 1),
        ):
            for code in (403, 429):
                with self.subTest(code=code, headers=headers):
                    self.assertEqual(smoke.rate_limit_wait(http_error(code, headers), NOW), wait)


class SmokeWiring(unittest.TestCase):
    def test_smoke_reads_fail_as_rate_limited_not_bare_403(self):
        fetch = Fetch(exhausted(4102444800))
        argv = ['smoke_public.py', 'sha256:' + 'a' * 64, '--local-image']
        with patch.object(core, 'public_bytes', fetch), patch.object(sys, 'argv', argv), \
                patch.object(sys, 'path', list(sys.path)), self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(SCRIPT), run_name='__main__')
        self.assertRegex(str(caught.exception.code), r'^rate limited: https://api\.github\.com/repos/'
                         r'atrinik/classic/releases/latest returned HTTP 403 and asks for ')
        self.assertEqual([call[0] for call in fetch.calls], [URL])
        self.assertNotIn('Authorization', fetch.calls[0][1][0])


if __name__ == '__main__':
    unittest.main()
