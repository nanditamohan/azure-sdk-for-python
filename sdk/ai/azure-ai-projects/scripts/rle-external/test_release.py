"""Focused tests for external wheel preparation and installation."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_rle_sdk as installer
import release

VERSION = "2.7.0.dev20261012000000"
SECOND_VERSION = "2.7.0.dev20261012000001"


def wheel_bytes(version=VERSION, *, extra="rle", module=True):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            f"azure_ai_projects-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: azure-ai-projects\nVersion: {version}\nProvides-Extra: {extra}\n",
        )
        if module:
            archive.writestr("azure/ai/projects/rle/environments/__init__.py", "")
    return output.getvalue()


def manifest_for(version=VERSION, data=None):
    data = wheel_bytes(version) if data is None else data
    filename = f"azure_ai_projects-{version}-py3-none-any.whl"
    return {
        "schema_version": 1, "package": "azure-ai-projects", "channel": "rle-external",
        "latest": version,
        "versions": {version: {
            "filename": filename,
            "url": (
                f"https://raw.githubusercontent.com/{installer.PUBLIC_REPOSITORY}/"
                f"{'a' * 40}/{installer.PUBLIC_PATH}/wheels/{version}/{filename}"
            ),
            "sha256": hashlib.sha256(data).hexdigest(),
        }},
    }


def public_checkout(tmp_path):
    checkout = tmp_path / "public"
    checkout.mkdir()
    release.git(checkout, "init", "--initial-branch=main")
    release.git(checkout, "config", "user.name", "Release test")
    release.git(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "baseline.txt").write_text("upstream\n")
    release.git(checkout, "add", "baseline.txt")
    release.git(checkout, "commit", "-m", "Upstream baseline")
    release.git(checkout, "remote", "add", "origin", f"https://github.com/{installer.PUBLIC_REPOSITORY}.git")
    release.git(checkout, "update-ref", "refs/remotes/origin/main", release.git(checkout, "rev-parse", "HEAD"))
    release.git(checkout, "switch", "-c", installer.PUBLIC_BRANCH)
    return checkout


def test_latest_and_explicit_versions_resolve_independently():
    manifest = manifest_for()
    manifest["versions"].update(manifest_for(SECOND_VERSION)["versions"])
    manifest["latest"] = SECOND_VERSION
    assert installer.resolve_release(manifest).version == SECOND_VERSION
    assert installer.resolve_release(manifest, VERSION).version == VERSION


@pytest.mark.parametrize("version", ["", "2.7.0", "missing", "../escape", SECOND_VERSION])
def test_explicit_invalid_or_missing_version_never_falls_back(version):
    with pytest.raises(ValueError):
        installer.resolve_release(manifest_for(), version)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("package", "other"), ("channel", "other"),
    ("latest", ""), ("versions", []),
])
def test_invalid_manifest_fails(field, value):
    manifest = manifest_for()
    manifest[field] = value
    with pytest.raises(ValueError):
        installer.resolve_release(manifest)


@pytest.mark.parametrize("field,value", [
    ("filename", "../../escape.whl"),
    ("url", "https://raw.githubusercontent.com/Azure/azure-sdk-for-python/main/sdk.whl"),
    ("url", "https://example.com/sdk.whl"),
    ("sha256", "not-a-hash"),
])
def test_invalid_artifact_entry_fails(field, value):
    manifest = manifest_for()
    manifest["versions"][VERSION][field] = value
    with pytest.raises(ValueError):
        installer.resolve_release(manifest)


def test_download_checks_hash_and_wheel_metadata(tmp_path):
    data = wheel_bytes()
    with patch.object(installer, "urlopen", return_value=io.BytesIO(data)):
        wheel = installer.download_wheel(installer.resolve_release(manifest_for()), tmp_path)
    assert wheel.read_bytes() == data


def test_checksum_failure_removes_download_and_never_installs(tmp_path):
    with patch.object(installer, "urlopen", return_value=io.BytesIO(b"tampered")):
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            installer.download_wheel(installer.resolve_release(manifest_for()), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kwargs", [
    {"version": SECOND_VERSION}, {"extra": "voice"}, {"module": False},
])
def test_wrong_wheel_contents_fail(tmp_path, kwargs):
    wheel = tmp_path / "sdk.whl"
    wheel.write_bytes(wheel_bytes(**kwargs))
    with pytest.raises(ValueError):
        installer.validate_wheel(wheel, VERSION)


def test_selector_precedence_and_empty_values_fail(monkeypatch, tmp_path):
    version_file = tmp_path / "sdk-version.txt"
    version_file.write_text(VERSION + "\n")
    monkeypatch.delenv("AZURE_AI_PROJECTS_RLE_VERSION", raising=False)
    assert installer.selected_version(None, None) == "latest"
    assert installer.selected_version(None, version_file) == VERSION
    monkeypatch.setenv("AZURE_AI_PROJECTS_RLE_VERSION", SECOND_VERSION)
    assert installer.selected_version(None, version_file) == SECOND_VERSION
    assert installer.selected_version(VERSION, version_file) == VERSION
    monkeypatch.setenv("AZURE_AI_PROJECTS_RLE_VERSION", "")
    with pytest.raises(ValueError):
        installer.resolve_release(manifest_for(), installer.selected_version(None, version_file))


def test_pip_receives_verified_wheel_extra_and_requirements(tmp_path, monkeypatch):
    manifest = tmp_path / "registry.json"
    manifest.write_text(json.dumps(manifest_for()))
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("openenv==0.6.0\n")
    monkeypatch.setattr(sys, "argv", [
        "install_rle_sdk.py", "--manifest", str(manifest), "--version", VERSION,
        "--requirements", str(requirements), "--index-url", "https://pypi.org/simple",
    ])
    with patch.object(installer, "urlopen", return_value=io.BytesIO(wheel_bytes())):
        with patch.object(installer.subprocess, "run") as run:
            installer.main()
    command = run.call_args.args[0]
    assert any(argument.startswith("azure-ai-projects[rle] @ file:") for argument in command)
    assert command[-4:] == ["--index-url", "https://pypi.org/simple", "--requirement", str(requirements)]
    assert run.call_args.kwargs["check"] is True


def test_releases_preserve_versions_and_pin_artifact_commits(tmp_path):
    checkout = public_checkout(tmp_path)
    source = tmp_path / "private-source"
    source.mkdir()
    release.ensure_public_checkout(checkout, source)
    first_wheel = tmp_path / f"azure_ai_projects-{VERSION}-py3-none-any.whl"
    first_wheel.write_bytes(wheel_bytes())
    first = release.prepare_release(checkout, first_wheel, VERSION)
    previous = first["versions"][VERSION].copy()
    artifact_commit = previous["url"].split("/")[5]
    path = f"{installer.PUBLIC_PATH}/wheels/{VERSION}/{first_wheel.name}"
    assert release.git(checkout, "cat-file", "-e", f"{artifact_commit}:{path}") == ""
    assert artifact_commit != release.git(checkout, "rev-parse", "HEAD")
    second_wheel = tmp_path / f"azure_ai_projects-{SECOND_VERSION}-py3-none-any.whl"
    second_wheel.write_bytes(wheel_bytes(SECOND_VERSION))
    second = release.prepare_release(checkout, second_wheel, SECOND_VERSION)
    assert second["versions"][VERSION] == previous
    assert second["latest"] == SECOND_VERSION
    assert set(second["versions"]) == {VERSION, SECOND_VERSION}
    release.ensure_public_checkout(checkout, source)
    with pytest.raises(ValueError, match="already released"):
        release.prepare_release(checkout, first_wheel, VERSION)
    assert not release.git(checkout, "status", "--porcelain")


@pytest.mark.parametrize("path", [
    "sdk/ai/azure-ai-projects/azure/ai/projects/rle/__init__.py",
    f"{installer.PUBLIC_PATH}/release.py",
    f"{installer.PUBLIC_PATH}/wheels/{VERSION}/secret.txt",
])
def test_private_source_and_unexpected_files_cannot_be_published(path):
    with pytest.raises(ValueError, match="non-distribution"):
        release.validate_public_paths([path])


def test_dirty_or_source_checkout_is_rejected(tmp_path):
    checkout = public_checkout(tmp_path)
    with pytest.raises(ValueError, match="separate"):
        release.ensure_public_checkout(checkout, checkout)
    (checkout / "private.txt").write_text("not for publication")
    with pytest.raises(ValueError, match="clean"):
        release.ensure_public_checkout(checkout, tmp_path / "source")


def test_first_use_initializes_public_checkout_only_from_upstream(tmp_path):
    checkout = tmp_path / "new-public"
    with patch.object(release.subprocess, "run") as run:
        with patch.object(release, "git") as git:
            git.side_effect = ["", "", "", str(checkout), f"https://github.com/{installer.PUBLIC_REPOSITORY}.git",
                               installer.PUBLIC_BRANCH, "", ""]
            release.ensure_public_checkout(checkout, tmp_path / "source")
    assert run.call_args.args[0][-2:] == [
        f"https://github.com/{installer.PUBLIC_REPOSITORY}.git", str(checkout),
    ]


def test_first_use_fetches_existing_distribution_branch(tmp_path):
    checkout = tmp_path / "new-public"
    with patch.object(release.subprocess, "run"):
        with patch.object(release, "git") as git:
            git.side_effect = [
                "", f"{'a' * 40}\trefs/heads/{installer.PUBLIC_BRANCH}", "", "",
                str(checkout), f"https://github.com/{installer.PUBLIC_REPOSITORY}.git",
                installer.PUBLIC_BRANCH, "", "",
            ]
            release.ensure_public_checkout(checkout, tmp_path / "source")
    assert any(
        call.args == (checkout, "switch", "-c", installer.PUBLIC_BRANCH, "FETCH_HEAD")
        for call in git.call_args_list
    )


def test_no_credentials_are_needed_for_prepare_and_missing_publish_auth_fails(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    failure = subprocess.CompletedProcess([], 128, "", "No credentials")
    with patch.object(release.subprocess, "run", return_value=failure):
        with pytest.raises(RuntimeError, match="Publishing requires"):
            release.github_token()


def test_account_selection_is_forwarded_without_exposing_credentials(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    credential = subprocess.CompletedProcess([], 0, "username=test-user\npassword=test-token\n", "")
    with patch.object(release.subprocess, "run", return_value=credential) as run:
        assert release.github_token("test-user") == "test-token"
    assert run.call_args.kwargs["input"] == "protocol=https\nhost=github.com\nusername=test-user\n\n"
    with pytest.raises(ValueError, match="username"):
        release.github_token("user\npassword=injected")


def test_publish_reuses_existing_pr_and_verifies_anonymous_downloads(tmp_path):
    checkout = public_checkout(tmp_path)
    manifest = manifest_for()
    public = checkout / installer.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text(json.dumps(manifest))
    release.git(checkout, "add", "--", installer.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "Public registry")
    real_git = release.git
    with patch.object(release, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
        with patch.object(installer, "download_wheel") as download:
            with patch.object(release, "github_api", return_value=[{"html_url": "https://github.com/Azure/azure-sdk-for-python/pull/1"}]) as api:
                with patch.object(release, "git", wraps=release.git) as git:
                    # Avoid a network push while keeping the source-isolation checks real.
                    git.side_effect = lambda root, *args, **kwargs: "" if args[0] == "push" else real_git(root, *args, **kwargs)
                    assert release.publish(checkout, "test-token").endswith("/pull/1")
    download.assert_called_once()
    api.assert_called_once()


def test_publish_creates_draft_pr_only_after_anonymous_verification(tmp_path):
    checkout = public_checkout(tmp_path)
    manifest = manifest_for()
    public = checkout / installer.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text(json.dumps(manifest))
    release.git(checkout, "add", "--", installer.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "Public registry")
    real_git = release.git
    events = []

    def api_call(token, path, data=None):
        events.append("create" if data else "lookup")
        if data:
            assert data["draft"] is True
            assert data["head"] == installer.PUBLIC_BRANCH
            assert "sujit-kamireddy" not in data["body"]
            return {"html_url": "https://github.com/Azure/azure-sdk-for-python/pull/2"}
        return []

    with patch.object(release, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
        with patch.object(installer, "download_wheel", side_effect=lambda *args: events.append("verify")):
            with patch.object(release, "github_api", side_effect=api_call):
                with patch.object(release, "git") as git:
                    git.side_effect = lambda root, *args, **kwargs: "" if args[0] == "push" else real_git(root, *args, **kwargs)
                    assert release.publish(checkout, "test-token").endswith("/pull/2")
    assert events == ["verify", "lookup", "create"]


def test_anonymous_manifest_mismatch_prevents_pr_creation(tmp_path):
    checkout = public_checkout(tmp_path)
    public = checkout / installer.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text(json.dumps(manifest_for()))
    release.git(checkout, "add", "--", installer.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "Public registry")
    real_git = release.git
    with patch.object(release, "urlopen", return_value=io.BytesIO(b"{}")):
        with patch.object(release, "github_api") as api:
            with patch.object(release, "git") as git:
                git.side_effect = lambda root, *args, **kwargs: "" if args[0] == "push" else real_git(root, *args, **kwargs)
                with pytest.raises(ValueError, match="differs"):
                    release.publish(checkout, "test-token")
    api.assert_not_called()


def test_source_changes_in_public_history_are_rejected(tmp_path):
    checkout = public_checkout(tmp_path)
    (checkout / "baseline.txt").write_text("private implementation\n")
    release.git(checkout, "add", "baseline.txt")
    release.git(checkout, "commit", "-m", "Must not publish")
    with pytest.raises(ValueError, match="non-distribution"):
        release.ensure_public_checkout(checkout, tmp_path / "source")


def test_build_stages_version_without_changing_source(tmp_path):
    package = tmp_path / "source"
    version_file = package / "azure" / "ai" / "projects" / "_version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('VERSION = "2.7.0"\n')
    tooling = package / "scripts"
    tooling.mkdir()
    (tooling / "private-release.py").write_text("private tooling\n")
    workspace = tmp_path / "build"
    workspace.mkdir()

    def build(command, **kwargs):
        staged = workspace / "package"
        assert (staged / version_file.relative_to(package)).read_text() == f'VERSION = "{VERSION}"\n'
        assert not (staged / "scripts").exists()
        wheels = workspace / "wheels"
        wheels.mkdir()
        (wheels / f"azure_ai_projects-{VERSION}-py3-none-any.whl").write_bytes(wheel_bytes())

    with patch.object(release.subprocess, "run", side_effect=build):
        wheel = release.build_wheel(package, VERSION, workspace)
    assert wheel.exists()
    assert version_file.read_text() == 'VERSION = "2.7.0"\n'
