#!/usr/bin/env python3
"""Fail-closed, resumable draft publication; semantic-release alone owns Git tags."""
# Copyright (c) 2026 Atrinik contributors. MIT licensed.
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPOSITORY = "atrinik/service-updater"
IMAGE = "ghcr.io/atrinik/service-updater"
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def run(*args: str, data: str | None = None) -> str:
    return subprocess.run(args, input=data, text=True, check=True,
                          stdout=subprocess.PIPE).stdout.strip()


def api(path: str, *, method: str = "GET", payload: dict | None = None):
    args = ["gh", "api", f"repos/{REPOSITORY}/{path}", "--method", method]
    if payload is not None:
        args += ["--input", "-"]
    return json.loads(run(*args, data=json.dumps(payload) if payload is not None else None))


def source() -> str:
    sha = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid source commit")
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Unexpected release repository")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Releases require main")
    if run("git", "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout is not the triggering source")
    current = run("git", "ls-remote", "origin", "refs/heads/main").split()
    if len(current) != 2 or current[0] != sha:
        raise ValueError("main advanced; recover the pending transaction explicitly before another release")
    return sha


def version() -> str:
    value = os.environ["RELEASE_VERSION"]
    if not VERSION_RE.fullmatch(value):
        raise ValueError("Invalid release version")
    return value


def marker(ver: str, sha: str) -> str:
    return f"<!-- service-updater-release:{sha}:{ver} -->"


def releases() -> list[dict]:
    result = []
    for page in range(1, 11):
        batch = api(f"releases?per_page=100&page={page}")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ValueError("Release inventory exceeds 1,000 entries; review pagination policy")


def pending() -> dict:
    sha = source()
    items = releases()
    by_tag = {item["tag_name"]: item for item in items}
    if len(by_tag) != len(items):
        raise ValueError("Duplicate release tags")
    # Never let semantic-release silently skip a tag from an interrupted publication.
    for tag in run("git", "tag", "--list", "v*").splitlines():
        if VERSION_RE.fullmatch(tag[1:]) and tag not in by_tag:
            raise ValueError(f"Orphan semantic tag {tag}; restore its original candidate before continuing")
    drafts = [item for item in items if item["draft"]]
    if len(drafts) > 1:
        raise ValueError("Multiple pending drafts require reconciliation")
    if not drafts:
        return {"version": None, "tagged": False}
    draft = drafts[0]
    ver = draft["tag_name"].removeprefix("v")
    if (not VERSION_RE.fullmatch(ver) or draft["target_commitish"] != sha
            or not draft.get("body", "").endswith(marker(ver, sha))):
        raise ValueError("Pending draft identity differs from current source; preserve it for explicit recovery")
    remote = remote_tag(ver)
    if remote is not None and remote != sha:
        raise ValueError("Pending semantic tag points to a different source")
    return {"version": ver, "tagged": remote is not None}


def remote_tag(ver: str) -> str | None:
    rows = run("git", "ls-remote", "origin", f"refs/tags/v{ver}", f"refs/tags/v{ver}^{{}}").splitlines()
    refs = dict(row.split()[::-1] for row in rows)
    return refs.get(f"refs/tags/v{ver}^{{}}", refs.get(f"refs/tags/v{ver}"))


def image_digest() -> str | None:
    reference = f"{IMAGE}:{version()}"
    proc = subprocess.run(["docker", "buildx", "imagetools", "inspect", reference,
                           "--format", "{{.Manifest.Digest}}"], text=True, capture_output=True)
    if proc.returncode:
        if f"{reference}: not found" in proc.stderr or "manifest unknown" in proc.stderr:
            return None
        raise ValueError("Unable to inspect release image; refusing to assume it is absent")
    digest = proc.stdout.strip()
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("Invalid registry index digest")
    run("docker", "pull", f"{IMAGE}@{digest}")
    details = json.loads(run("docker", "image", "inspect", f"{IMAGE}@{digest}"))[0]
    labels = details["Config"].get("Labels", {})
    required = {"org.opencontainers.image.version": version(),
                "org.opencontainers.image.revision": source(),
                "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
                "org.opencontainers.image.licenses": "MIT"}
    if any(labels.get(key) != value for key, value in required.items()):
        raise ValueError("Existing version image has a different source or release identity")
    # Labels identify a candidate; only existing trusted provenance authorizes reuse.
    # Do not re-attest an untrusted pre-existing image under this workflow identity.
    run("gh", "attestation", "verify", f"oci://{IMAGE}@{digest}",
        "--repo", REPOSITORY, "--signer-workflow", f"{REPOSITORY}/.github/workflows/release.yml",
        "--source-digest", source(), "--deny-self-hosted-runners")
    return digest


def manifest() -> None:
    sha = source()
    ver = version()
    digest = os.environ["IMAGE_DIGEST"]
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError("Invalid image digest")
    archive = Path(f"dist/atrinik-service-updater-{ver}.tar.gz")
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    value = {"schema": 1, "repository": REPOSITORY, "version": ver, "source": sha,
             "image": f"{IMAGE}@{digest}",
             "archive": {"name": archive.name, "sha256": archive_digest, "size": archive.stat().st_size}}
    Path("dist/release-manifest.json").write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    Path("dist/SHA256SUMS").write_text(f"{archive_digest}  {archive.name}\n")


def candidate() -> tuple[dict, dict[str, dict]]:
    sha = source()
    ver = version()
    value = json.loads(Path("dist/release-manifest.json").read_text())
    if (value.get("schema") != 1 or value.get("repository") != REPOSITORY
            or value.get("version") != ver or value.get("source") != sha
            or value.get("image") != f"{IMAGE}@{os.environ['IMAGE_DIGEST']}"):
        raise ValueError("Candidate identity mismatch")
    archive = Path(f"dist/atrinik-service-updater-{ver}.tar.gz")
    expected = {}
    for path in [archive, Path("dist/release-manifest.json"), Path("dist/SHA256SUMS")]:
        if not path.is_file() or path.is_symlink():
            raise ValueError("Candidate asset must be a regular file")
        expected[path.name] = {"size": path.stat().st_size,
                               "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
    if value.get("archive") != {"name": archive.name, "size": expected[archive.name]["size"],
                                "sha256": expected[archive.name]["digest"].removeprefix("sha256:")}:
        raise ValueError("Candidate archive differs from its manifest")
    if Path("dist/SHA256SUMS").read_text() != f"{value['archive']['sha256']}  {archive.name}\n":
        raise ValueError("Candidate checksum file mismatch")
    return value, expected


def assert_assets(actual: list[dict], expected: dict, *, complete: bool) -> set[str]:
    seen = set()
    for asset in actual:
        name = asset["name"]
        if name in seen or name not in expected:
            raise ValueError("Unexpected or duplicate release asset")
        seen.add(name)
        if (asset.get("state") != "uploaded" or asset.get("size") != expected[name]["size"]
                or asset.get("digest") != expected[name]["digest"]):
            raise ValueError(f"Release asset mismatch: {name}; existing assets are never replaced")
    if complete and seen != set(expected):
        raise ValueError("Release assets are incomplete")
    return seen


def release_identity(item: dict, ver: str, sha: str) -> None:
    if (item["tag_name"] != f"v{ver}" or item["target_commitish"] != sha
            or item.get("prerelease") or not item.get("body", "").endswith(marker(ver, sha))):
        raise ValueError("Existing release identity mismatch")


def draft() -> None:
    value, expected = candidate()
    ver, sha = value["version"], value["source"]
    existing = [item for item in releases() if item["tag_name"] == f"v{ver}"]
    if len(existing) > 1:
        raise ValueError("Duplicate release transaction")
    if existing:
        item = existing[0]
        release_identity(item, ver, sha)
        if not item["draft"]:
            raise ValueError("Release already published; do not rerun semantic version creation")
    else:
        notes = Path("dist/release-notes.md").read_text()
        item = api("releases", method="POST", payload={"tag_name": f"v{ver}",
                   "target_commitish": sha, "name": f"v{ver}", "draft": True,
                   "prerelease": False, "body": notes + "\n\n" + marker(ver, sha)})
    seen = assert_assets(item.get("assets", []), expected, complete=False)
    for name in sorted(set(expected) - seen):
        run("gh", "release", "upload", f"v{ver}", f"dist/{name}", "--repo", REPOSITORY)
    fresh = api(f"releases/{item['id']}")
    release_identity(fresh, ver, sha)
    assert_assets(fresh["assets"], expected, complete=True)
    source()  # Rebind immediately before semantic-release creates its tag.


def publish() -> None:
    value, expected = candidate()
    ver, sha = value["version"], value["source"]
    if remote_tag(ver) != sha:
        raise ValueError("Publication requires the exact semantic-release-owned Git tag")
    items = [item for item in releases() if item["tag_name"] == f"v{ver}"]
    if len(items) != 1:
        raise ValueError("Publication requires exactly one prepared draft")
    item = api(f"releases/{items[0]['id']}")
    release_identity(item, ver, sha)
    assert_assets(item["assets"], expected, complete=True)
    if image_digest() != os.environ["IMAGE_DIGEST"]:
        raise ValueError("Versioned image changed before publication")
    source()
    if item["draft"]:
        item = api(f"releases/{item['id']}", method="PATCH", payload={"draft": False, "make_latest": "true"})
    release_identity(item, ver, sha)
    if item["draft"]:
        raise ValueError("Release publication did not complete")
    assert_assets(item["assets"], expected, complete=True)


def main() -> None:
    command = sys.argv[1]
    if command == "pending":
        print(json.dumps(pending()))
    elif command == "image":
        source()
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"digest={image_digest() or ''}\n")
    elif command == "manifest":
        manifest()
    elif command == "prepare":
        draft()
    elif command == "publish":
        publish()
    else:
        raise ValueError("Unknown release operation")


if __name__ == "__main__":
    main()
