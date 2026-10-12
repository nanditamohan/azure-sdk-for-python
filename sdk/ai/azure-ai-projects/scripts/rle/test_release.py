"""Focused tests for SDK beta release preparation and publication."""

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
import release

VERSION = "2.7.0b1"
SECOND_VERSION = "2.7.0b2"
LEGACY_VERSION = "2.7.0.dev20261012000000"


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
                f"https://raw.githubusercontent.com/{release.PUBLIC_REPOSITORY}/"
                f"{'a' * 40}/{release.PUBLIC_PATH}/wheels/{version}/{filename}"
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
    release.git(checkout, "remote", "add", "origin", f"https://github.com/{release.PUBLIC_REPOSITORY}.git")
    release.git(checkout, "update-ref", "refs/remotes/origin/main", release.git(checkout, "rev-parse", "HEAD"))
    release.git(checkout, "switch", "-c", release.PUBLIC_BRANCH)
    return checkout


def test_latest_and_explicit_versions_resolve_independently():
    manifest = manifest_for()
    manifest["versions"].update(manifest_for(SECOND_VERSION)["versions"])
    manifest["latest"] = SECOND_VERSION
    assert release.resolve_release(manifest).version == SECOND_VERSION
    assert release.resolve_release(manifest, VERSION).version == VERSION


@pytest.mark.parametrize("version", ["", "2.7.0", "missing", "../escape", SECOND_VERSION])
def test_explicit_invalid_or_missing_version_never_falls_back(version):
    with pytest.raises(ValueError):
        release.resolve_release(manifest_for(), version)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("package", "other"), ("channel", "other"),
    ("latest", ""), ("versions", []),
])
def test_invalid_manifest_fails(field, value):
    manifest = manifest_for()
    manifest[field] = value
    with pytest.raises(ValueError):
        release.resolve_release(manifest)


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
        release.resolve_release(manifest)


def test_download_checks_hash_and_wheel_metadata(tmp_path):
    data = wheel_bytes()
    with patch.object(release, "urlopen", return_value=io.BytesIO(data)):
        wheel = release.download_wheel(release.resolve_release(manifest_for()), tmp_path)
    assert wheel.read_bytes() == data


def test_checksum_failure_removes_download(tmp_path):
    with patch.object(release, "urlopen", return_value=io.BytesIO(b"tampered")):
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            release.download_wheel(release.resolve_release(manifest_for()), tmp_path)
    assert not list(tmp_path.iterdir())


def test_release_tooling_does_not_depend_on_a_consumer_installer():
    assert not (Path(release.__file__).parent / "install_rle_sdk.py").exists()


@pytest.mark.parametrize("versions,base,bump,expected", [
    ([], "2.7.0", "patch", "2.7.1b1"),
    ([LEGACY_VERSION], "2.7.0", "patch", "2.7.1b1"),
    ([VERSION, SECOND_VERSION, LEGACY_VERSION], "2.7.0", "patch", "2.7.1b1"),
    (["2.7.2b1", "2.7.9b1", "2.7.10b1"], "2.7.0", "patch", "2.7.11b1"),
    ([VERSION, "2.8.0b9"], "2.7.0", "patch", "2.8.1b1"),
    ([VERSION], "2.9.0", "patch", "2.9.1b1"),
    (["2.7.9b1"], "2.7.0", "minor", "2.8.0b1"),
    (["2.7.9b1"], "2.7.0", "major", "3.0.0b1"),
])
def test_version_bumping_uses_highest_core_and_resets_smaller_components(tmp_path, versions, base, bump, expected):
    if versions:
        manifest = manifest_for(versions[0])
        for version in versions[1:]:
            manifest["versions"].update(manifest_for(version)["versions"])
        public = tmp_path / release.PUBLIC_PATH
        public.mkdir(parents=True)
        (public / "registry.json").write_text(json.dumps(manifest))
    assert release.next_version(tmp_path, base, bump) == expected


def test_version_bumping_rejects_invalid_existing_registry(tmp_path):
    public = tmp_path / release.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text("{}")
    with pytest.raises(ValueError, match="Unsupported"):
        release.next_version(tmp_path, "2.7.0")


@pytest.mark.parametrize("base,bump", [
    ("2.7", "patch"), ("2.7.0b1", "patch"), ("2.7.0", "beta"), ("2.7.0", ""),
])
def test_version_bumping_rejects_invalid_inputs(tmp_path, base, bump):
    with pytest.raises(ValueError):
        release.next_version(tmp_path, base, bump)


@pytest.mark.parametrize("version", [VERSION, SECOND_VERSION, LEGACY_VERSION])
def test_public_paths_allow_beta_and_retained_development_artifacts(version):
    release.validate_public_paths([
        f"{release.PUBLIC_PATH}/registry.json",
        f"{release.PUBLIC_PATH}/wheels/{version}/azure_ai_projects-{version}-py3-none-any.whl",
    ])


@pytest.fixture
def cli_source(tmp_path, monkeypatch):
    package = tmp_path / "source"
    version_file = package / "azure" / "ai" / "projects" / "_version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('VERSION = "2.7.0"\n')
    monkeypatch.setattr(release, "PACKAGE", package)
    monkeypatch.setattr(release, "ensure_public_checkout", lambda *args: None)
    monkeypatch.setattr(
        release, "git",
        lambda root, *args: str(package) if args[0] == "rev-parse" else "",
    )
    return package


@pytest.mark.parametrize("bump", ["", "beta", "dev", VERSION])
def test_cli_rejects_invalid_bumps_before_building(tmp_path, monkeypatch, cli_source, bump):
    monkeypatch.setattr(sys, "argv", [
        "release.py", "--bump", bump,
    ])
    with patch.object(release, "build_wheel") as build:
        with pytest.raises(SystemExit) as error:
            release.main()
    assert error.value.code == 2
    build.assert_not_called()


@pytest.mark.parametrize("bump,expected", [
    (None, "2.7.1b1"), ("patch", "2.7.1b1"), ("minor", "2.8.0b1"), ("major", "3.0.0b1"),
])
def test_cli_builds_patch_by_default_or_the_selected_bump(
    tmp_path, monkeypatch, cli_source, capsys, bump, expected,
):
    arguments = ["release.py"]
    if bump is not None:
        arguments.extend(["--bump", bump])
    monkeypatch.setattr(sys, "argv", arguments)
    with (
        patch.object(release, "ensure_public_checkout") as ensure,
        patch.object(release, "build_wheel", return_value=tmp_path / "sdk.whl") as build,
        patch.object(release, "prepare_release") as prepare,
        patch.object(release, "github_token", return_value="test-token"),
        patch.object(release, "publish", return_value=release.PUBLIC_PR_URL) as publish,
    ):
        release.main()
    checkout = ensure.call_args.args[0]
    ensure.assert_called_once_with(checkout, cli_source)
    assert build.call_args.args[1] == expected
    assert checkout.parent.name.startswith("rle-sdk-release-")
    assert checkout != cli_source.parent / "azure-sdk-for-python-rle-external"
    prepare.assert_called_once_with(checkout, tmp_path / "sdk.whl", expected)
    publish.assert_called_once_with(checkout, "test-token")
    assert not checkout.parent.exists()
    output = capsys.readouterr().out
    assert f"Published RLE SDK {expected}. Registry latest updated to {expected}." in output
    assert f"Registry: {release.MANIFEST_URL}" in output
    assert f"PR: {release.PUBLIC_PR_URL}" in output


@pytest.mark.parametrize("failure", [
    "ensure_public_checkout", "build_wheel", "prepare_release", "publish",
])
def test_temporary_checkout_is_removed_when_release_fails(
    tmp_path, monkeypatch, cli_source, capsys, failure,
):
    monkeypatch.setattr(sys, "argv", ["release.py"])
    with (
        patch.object(release, "ensure_public_checkout") as ensure,
        patch.object(release, "build_wheel", return_value=tmp_path / "sdk.whl") as build,
        patch.object(release, "prepare_release") as prepare,
        patch.object(release, "github_token", return_value="test-token"),
        patch.object(release, "publish") as publish,
    ):
        {"ensure_public_checkout": ensure, "build_wheel": build,
         "prepare_release": prepare, "publish": publish}[failure].side_effect = ValueError("Release failed")
        with pytest.raises(ValueError, match="Release failed"):
            release.main()
    assert not ensure.call_args.args[0].parent.exists()
    assert "Published RLE SDK" not in capsys.readouterr().out


@pytest.mark.parametrize("option", ["--publish", "--publish-only", "--public-checkout"])
def test_cli_rejects_removed_workflows(monkeypatch, option):
    monkeypatch.setattr(sys, "argv", ["release.py", option])
    with patch.object(release, "github_token") as token:
        with pytest.raises(SystemExit) as error:
            release.main()
    assert error.value.code == 2
    token.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"version": SECOND_VERSION}, {"extra": "voice"}, {"module": False},
])
def test_wrong_wheel_contents_fail(tmp_path, kwargs):
    wheel = tmp_path / "sdk.whl"
    wheel.write_bytes(wheel_bytes(**kwargs))
    with pytest.raises(ValueError):
        release.validate_wheel(wheel, VERSION)


@pytest.mark.parametrize("first_version", [VERSION, LEGACY_VERSION])
def test_releases_preserve_versions_and_pin_artifact_commits(tmp_path, first_version):
    checkout = public_checkout(tmp_path)
    source = tmp_path / "private-source"
    source.mkdir()
    release.ensure_public_checkout(checkout, source)
    first_wheel = tmp_path / f"azure_ai_projects-{first_version}-py3-none-any.whl"
    first_wheel.write_bytes(wheel_bytes(first_version))
    first = release.prepare_release(checkout, first_wheel, first_version)
    previous = first["versions"][first_version].copy()
    artifact_commit = previous["url"].split("/")[5]
    path = f"{release.PUBLIC_PATH}/wheels/{first_version}/{first_wheel.name}"
    assert release.git(checkout, "cat-file", "-e", f"{artifact_commit}:{path}") == ""
    assert artifact_commit != release.git(checkout, "rev-parse", "HEAD")
    second_wheel = tmp_path / f"azure_ai_projects-{SECOND_VERSION}-py3-none-any.whl"
    second_wheel.write_bytes(wheel_bytes(SECOND_VERSION))
    second = release.prepare_release(checkout, second_wheel, SECOND_VERSION)
    assert second["versions"][first_version] == previous
    assert second["latest"] == SECOND_VERSION
    assert set(second["versions"]) == {first_version, SECOND_VERSION}
    assert set(release.git(checkout, "diff", "--name-only", "origin/main", "HEAD").splitlines()) == {
        f"{release.PUBLIC_PATH}/registry.json",
        f"{release.PUBLIC_PATH}/wheels/{first_version}/{first_wheel.name}",
        f"{release.PUBLIC_PATH}/wheels/{SECOND_VERSION}/{second_wheel.name}",
    }
    release.ensure_public_checkout(checkout, source)
    with pytest.raises(ValueError, match="already released"):
        release.prepare_release(checkout, first_wheel, first_version)
    assert not release.git(checkout, "status", "--porcelain")


@pytest.mark.parametrize("path", [
    "sdk/ai/azure-ai-projects/azure/ai/projects/rle/__init__.py",
    f"{release.PUBLIC_PATH}/release.py",
    f"{release.PUBLIC_PATH}/install_rle_sdk.py",
    f"{release.PUBLIC_PATH}/README.md",
    f"{release.PUBLIC_PATH}/wheels/{VERSION}/secret.txt",
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
            git.side_effect = ["", "", "", str(checkout), f"https://github.com/{release.PUBLIC_REPOSITORY}.git",
                               release.PUBLIC_BRANCH, "", "a" * 40, ""]
            release.ensure_public_checkout(checkout, tmp_path / "source")
    assert run.call_args.args[0][-2:] == [
        f"https://github.com/{release.PUBLIC_REPOSITORY}.git", str(checkout),
    ]


def test_first_use_fetches_existing_distribution_branch(tmp_path):
    checkout = tmp_path / "new-public"
    with patch.object(release.subprocess, "run"):
        with patch.object(release, "git") as git:
            git.side_effect = [
                "", f"{'a' * 40}\trefs/heads/{release.PUBLIC_BRANCH}", "", "",
                str(checkout), f"https://github.com/{release.PUBLIC_REPOSITORY}.git",
                release.PUBLIC_BRANCH, "", "a" * 40, "",
            ]
            release.ensure_public_checkout(checkout, tmp_path / "source")
    assert any(
        call.args == (checkout, "switch", "-c", release.PUBLIC_BRANCH, "FETCH_HEAD")
        for call in git.call_args_list
    )


def test_missing_publish_auth_fails(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    failure = subprocess.CompletedProcess([], 128, "", "No credentials")
    with patch.object(release.subprocess, "run", return_value=failure):
        with pytest.raises(RuntimeError, match="Publishing requires"):
            release.github_token()


def test_shallow_checkout_deepens_history_to_find_the_pr_base(tmp_path):
    changed = f"{release.PUBLIC_PATH}/registry.json"
    with patch.object(release, "git") as git:
        git.side_effect = [
            subprocess.CalledProcessError(1, ["git", "merge-base"]),
            "", "a" * 40, changed,
        ]
        assert release.public_changes(tmp_path) == [changed]
    assert git.call_args_list[1].args == (
        tmp_path, "fetch", "--deepen=50", "origin", "main", release.PUBLIC_BRANCH,
    )
    assert git.call_args_list[-1].args == (
        tmp_path, "diff", "--name-only", "origin/main...HEAD",
    )


def test_pr_base_lookup_does_not_hide_git_errors(tmp_path):
    with patch.object(release, "git", side_effect=subprocess.CalledProcessError(128, ["git"])) as git:
        with pytest.raises(subprocess.CalledProcessError):
            release.public_changes(tmp_path)
    git.assert_called_once()


def test_advancing_upstream_main_does_not_count_as_private_pr_changes(tmp_path):
    checkout = public_checkout(tmp_path)
    release.git(checkout, "switch", "main")
    (checkout / "baseline.txt").write_text("Upstream-only change.\n")
    release.git(checkout, "add", "baseline.txt")
    release.git(checkout, "commit", "-m", "Advance upstream main")
    release.git(checkout, "update-ref", "refs/remotes/origin/main", release.git(checkout, "rev-parse", "HEAD"))
    release.git(checkout, "switch", release.PUBLIC_BRANCH)
    wheel = tmp_path / f"azure_ai_projects-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(wheel_bytes())
    release.prepare_release(checkout, wheel, VERSION)
    release.ensure_public_checkout(checkout, tmp_path / "private-source")
    assert set(release.public_changes(checkout)) == {
        f"{release.PUBLIC_PATH}/registry.json",
        f"{release.PUBLIC_PATH}/wheels/{VERSION}/{wheel.name}",
    }


def test_real_shallow_clone_deepens_and_temporary_git_files_are_cleaned_up(tmp_path):
    upstream = public_checkout(tmp_path)
    registry_path = f"{release.PUBLIC_PATH}/registry.json"
    registry = upstream / registry_path
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps(manifest_for()))
    release.git(upstream, "add", "--", registry_path)
    release.git(upstream, "commit", "-m", "Public registry")
    release.git(upstream, "switch", "main")
    (upstream / "baseline.txt").write_text("Upstream-only change.\n")
    release.git(upstream, "add", "baseline.txt")
    release.git(upstream, "commit", "-m", "Advance upstream main")
    with release.tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        checkout = (Path(temporary) / "public").resolve()
        release.git(
            tmp_path, "clone", "--depth=1", "--branch", release.PUBLIC_BRANCH,
            upstream.as_uri(), str(checkout),
        )
        release.git(checkout, "fetch", "--depth=1", "origin", "main:refs/remotes/origin/main")
        with pytest.raises(subprocess.CalledProcessError):
            release.git(checkout, "merge-base", "origin/main", "HEAD")
        assert release.public_changes(checkout) == [registry_path]
        release.validate_public_paths(release.public_changes(checkout))
    assert not Path(temporary).exists()


def test_account_selection_is_forwarded_without_exposing_credentials(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    credential = subprocess.CompletedProcess([], 0, "username=test-user\npassword=test-token\n", "")
    with patch.object(release.subprocess, "run", return_value=credential) as run:
        assert release.github_token("test-user") == "test-token"
    assert run.call_args.kwargs["input"] == "protocol=https\nhost=github.com\nusername=test-user\n\n"
    with pytest.raises(ValueError, match="username"):
        release.github_token("user\npassword=injected")


def test_publish_updates_existing_distribution_and_verifies_anonymous_downloads(tmp_path):
    checkout = public_checkout(tmp_path)
    manifest = manifest_for()
    public = checkout / release.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text(json.dumps(manifest))
    release.git(checkout, "add", "--", release.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "Public registry")
    real_git = release.git
    with patch.object(release, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
        with patch.object(release, "download_wheel") as download:
            with patch.object(release, "git", wraps=release.git) as git:
                git.side_effect = lambda root, *args, **kwargs: "" if args[0] == "push" else real_git(root, *args, **kwargs)
                assert release.publish(checkout, "test-token") == release.PUBLIC_PR_URL
    download.assert_called_once()


def test_publish_verifies_all_retained_approved_versions(tmp_path):
    checkout = public_checkout(tmp_path)
    manifest = manifest_for()
    manifest["versions"].update(manifest_for("2.7.1b1")["versions"])
    manifest["latest"] = "2.7.1b1"
    public = checkout / release.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text(json.dumps(manifest))
    release.git(checkout, "add", "--", release.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "Public registry")
    real_git = release.git
    with patch.object(release, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
        with patch.object(release, "download_wheel") as download:
            with patch.object(release, "git") as git:
                git.side_effect = lambda root, *args, **kwargs: "" if args[0] == "push" else real_git(root, *args, **kwargs)
                assert release.publish(checkout, "test-token") == release.PUBLIC_PR_URL
    assert [call.args[0].version for call in download.call_args_list] == [VERSION, "2.7.1b1"]


def test_anonymous_manifest_mismatch_stops_publication_verification(tmp_path):
    checkout = public_checkout(tmp_path)
    public = checkout / release.PUBLIC_PATH
    public.mkdir(parents=True)
    (public / "registry.json").write_text(json.dumps(manifest_for()))
    release.git(checkout, "add", "--", release.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "Public registry")
    real_git = release.git
    with patch.object(release, "urlopen", return_value=io.BytesIO(b"{}")):
        with patch.object(release, "download_wheel") as download:
            with patch.object(release, "git") as git:
                git.side_effect = lambda root, *args, **kwargs: "" if args[0] == "push" else real_git(root, *args, **kwargs)
                with pytest.raises(ValueError, match="differs"):
                    release.publish(checkout, "test-token")
    download.assert_not_called()


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
