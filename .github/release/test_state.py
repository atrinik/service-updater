# Copyright (c) 2026 Atrinik contributors. MIT licensed.
"""Release transaction failures must preserve original tags, assets and images."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release_state", Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class ReleaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp.name)
        Path("dist").mkdir()
        Path("dist/atrinik-service-updater-1.0.0.tar.gz").write_bytes(b"immutable archive fixture")
        Path("dist/release-notes.md").write_text("Release notes")
        self.env = patch.dict(os.environ, {"RELEASE_VERSION": "1.0.0", "IMAGE_DIGEST": DIGEST,
                                          "GITHUB_SHA": SHA, "GITHUB_REPOSITORY": state.REPOSITORY,
                                          "GITHUB_REF": "refs/heads/main"})
        self.env.start()
        self.source = patch.object(state, "source", return_value=SHA)
        self.source.start()
        state.manifest()
        self.items = []
        self.mutations = []
        self.fail_upload = None
        self.lose_publication_response = False
        self.api_mock = patch.object(state, "api", side_effect=self.api)
        self.api_mock.start()
        self.run_mock = patch.object(state, "run", side_effect=self.command)
        self.run_mock.start()

    def tearDown(self):
        self.run_mock.stop()
        self.api_mock.stop()
        self.source.stop()
        self.env.stop()
        os.chdir(self.old_cwd)
        self.temp.cleanup()

    def api(self, path, *, method="GET", payload=None):
        if method == "GET" and path.startswith("releases?"):
            return copy.deepcopy(self.items)
        if method == "GET" and path == "releases/1":
            return copy.deepcopy(self.items[0])
        if method == "POST" and path == "releases":
            self.mutations.append((method, copy.deepcopy(payload)))
            self.items.append({**payload, "id": 1, "assets": []})
            return copy.deepcopy(self.items[0])
        if method == "PATCH" and path == "releases/1":
            self.mutations.append((method, copy.deepcopy(payload)))
            self.items[0].update(payload)
            if self.lose_publication_response:
                self.lose_publication_response = False
                raise OSError("Lost response after publication")
            return copy.deepcopy(self.items[0])
        raise AssertionError((path, method, payload))

    def command(self, *args, **kwargs):
        if args[:3] == ("gh", "release", "upload"):
            name = Path(args[4]).name
            if name == self.fail_upload:
                raise OSError("Injected upload interruption")
            _, expected = state.candidate()
            self.items[0]["assets"].append({"name": name, "state": "uploaded", **expected[name]})
            self.mutations.append(("upload", name))
            return ""
        if args[:3] == ("git", "tag", "--list"):
            return ""
        if args[:2] == ("git", "ls-remote"):
            return ""
        raise AssertionError(args)

    def publish(self):
        with patch.object(state, "remote_tag", return_value=SHA), \
             patch.object(state, "image_digest", return_value=DIGEST):
            state.publish()

    def test_draft_assets_complete_before_any_tag_or_publication(self):
        state.draft()
        self.assertTrue(self.items[0]["draft"])
        self.assertEqual(len(self.items[0]["assets"]), 3)
        self.assertEqual([operation for operation, _ in self.mutations], ["POST", "upload", "upload", "upload"])
        self.assertEqual(state.pending(), {"version": "1.0.0", "tagged": False})

    def test_partial_upload_resumes_only_missing_original_assets(self):
        self.fail_upload = "release-manifest.json"
        with self.assertRaises(OSError):
            state.draft()
        retained = copy.deepcopy(self.items[0]["assets"])
        self.fail_upload = None
        state.draft()
        self.assertEqual(self.items[0]["assets"][:len(retained)], retained)
        self.assertEqual(len(self.items), 1)
        self.assertEqual(len(self.items[0]["assets"]), 3)
        self.assertEqual(sum(operation == "POST" for operation, _ in self.mutations), 1)

    def test_conflicting_uploaded_bytes_are_never_replaced(self):
        state.draft()
        self.items[0]["assets"][0]["digest"] = "sha256:" + "c" * 64
        before = copy.deepcopy(self.mutations)
        with self.assertRaisesRegex(ValueError, "asset mismatch"):
            state.draft()
        self.assertEqual(self.mutations, before)

    def test_tag_push_crash_recovers_existing_complete_draft(self):
        state.draft()
        with patch.object(state, "remote_tag", return_value=SHA):
            self.assertEqual(state.pending(), {"version": "1.0.0", "tagged": True})
        self.publish()
        self.assertFalse(self.items[0]["draft"])

    def test_lost_publication_response_is_idempotent(self):
        state.draft()
        self.lose_publication_response = True
        with self.assertRaises(OSError):
            self.publish()
        self.publish()
        self.assertEqual(sum(operation == "PATCH" for operation, _ in self.mutations), 1)

    def test_wrong_tag_refuses_publication(self):
        state.draft()
        with patch.object(state, "remote_tag", return_value="c" * 40):
            with self.assertRaisesRegex(ValueError, "exact semantic-release-owned"):
                state.publish()
        self.assertTrue(self.items[0]["draft"])

    def test_changed_image_refuses_publication(self):
        state.draft()
        with patch.object(state, "remote_tag", return_value=SHA), \
             patch.object(state, "image_digest", return_value="sha256:" + "c" * 64):
            with self.assertRaisesRegex(ValueError, "image changed"):
                state.publish()
        self.assertTrue(self.items[0]["draft"])

    def test_incomplete_assets_refuse_publication(self):
        state.draft()
        self.items[0]["assets"].pop()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish()

    def test_old_source_draft_blocks_new_release(self):
        state.draft()
        self.items[0]["target_commitish"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "identity differs"):
            state.pending()

    def test_orphan_tag_does_not_silently_advance(self):
        with patch.object(state, "run", return_value="v1.0.0"):
            with self.assertRaisesRegex(ValueError, "Orphan semantic tag"):
                state.pending()

    def test_tampered_candidate_archive_is_rejected(self):
        Path("dist/atrinik-service-updater-1.0.0.tar.gz").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "archive differs"):
            state.draft()
        self.assertFalse(self.mutations)

    def test_duplicate_assets_are_rejected(self):
        state.draft()
        self.items[0]["assets"].append(copy.deepcopy(self.items[0]["assets"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.publish()

    def test_foreign_draft_marker_is_rejected(self):
        state.draft()
        self.items[0]["body"] = "Someone else's draft"
        with self.assertRaisesRegex(ValueError, "identity differs"):
            state.pending()

    def test_new_main_head_fails_at_point_of_use(self):
        self.source.stop()
        try:
            with patch.object(state, "run", side_effect=[SHA, "c" * 40 + "\trefs/heads/main"]):
                with self.assertRaisesRegex(ValueError, "main advanced"):
                    state.source()
        finally:
            self.source.start()

    def test_authentication_failure_is_not_absent_image(self):
        proc = subprocess.CompletedProcess([], 1, "", "unauthorized: authentication required")
        with patch.object(state.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(ValueError, "refusing to assume"):
                state.image_digest()

    def test_existing_image_with_forged_labels_needs_original_provenance(self):
        labels = {"org.opencontainers.image.version": "1.0.0",
                  "org.opencontainers.image.revision": SHA,
                  "org.opencontainers.image.source": f"https://github.com/{state.REPOSITORY}",
                  "org.opencontainers.image.licenses": "MIT"}
        proc = subprocess.CompletedProcess([], 0, DIGEST, "")
        with patch.object(state.subprocess, "run", return_value=proc), \
             patch.object(state, "run", side_effect=["", json.dumps([{"Config": {"Labels": labels}}]),
                                                     subprocess.CalledProcessError(1, "gh attestation verify")]) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                state.image_digest()
            self.assertEqual(command.call_args.args[:3], ("gh", "attestation", "verify"))
            self.assertIn("--deny-self-hosted-runners", command.call_args.args)

    def test_missing_image_can_be_built_once(self):
        proc = subprocess.CompletedProcess([], 1, "", f"{state.IMAGE}:1.0.0: not found")
        with patch.object(state.subprocess, "run", return_value=proc):
            self.assertIsNone(state.image_digest())


if __name__ == "__main__":
    unittest.main()
