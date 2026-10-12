"""Focused tests for local SDK beta release preparation."""

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


@pytest.fixture(autouse=True)
def local_public_fetch(monkeypatch):
    real_git = release.git
    public_url = f"https://github.com/{release.PUBLIC_REPOSITORY}.git"

    def git(checkout, *arguments):
        if arguments[0] == "fetch" and real_git(checkout, "remote", "get-url", "origin") == public_url:
            return real_git(
                checkout, "-c", f"url.{checkout.resolve().as_uri()}.insteadOf={public_url}",
                *arguments,
            )
        return real_git(checkout, *arguments)

    monkeypatch.setattr(release, "git", git)


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
        "schema_version": 1, "package": "azure-ai-projects", "channel": "rle",
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


def test_final_public_registry_contract(tmp_path):
    manifest = {
        "schema_version": 1,
        "package": "azure-ai-projects",
        "channel": "rle",
        "latest": "2.7.0b1",
        "versions": {"2.7.0b1": {
            "filename": "azure_ai_projects-2.7.0b1-py3-none-any.whl",
            "url": (
                "https://raw.githubusercontent.com/Azure/azure-sdk-for-python/"
                "rle-releases/sdk/ai/azure-ai-projects/rle/wheels/2.7.0b1/"
                "azure_ai_projects-2.7.0b1-py3-none-any.whl"
            ),
            "sha256": "004b027dc2629b834b1b957bc2b1b94f64c4fc0de901d2c90b8a1a21f5669979",
        }},
    }
    assert release.PUBLIC_BRANCH == "rle-releases"
    assert release.PUBLIC_PATH == "sdk/ai/azure-ai-projects/rle"
    assert release.resolve_release(manifest).version == "2.7.0b1"
    assert release.resolve_release(manifest, "2.7.0b1").version == "2.7.0b1"
    registry = tmp_path / "sdk" / "ai" / "azure-ai-projects" / "rle" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps(manifest))
    assert release.read_registry(tmp_path) == manifest
    assert release.next_version(tmp_path, "2.7.0") == "2.7.1b1"


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
        "release.py", "--public-checkout", str(tmp_path / "public"), "--bump", bump,
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
    checkout = tmp_path / "public"
    checkout.mkdir()
    arguments = ["release.py", "--public-checkout", str(checkout)]
    if bump is not None:
        arguments.extend(["--bump", bump])
    monkeypatch.setattr(sys, "argv", arguments)
    with (
        patch.object(release, "ensure_public_checkout") as ensure,
        patch.object(release, "build_wheel", return_value=tmp_path / "sdk.whl") as build,
        patch.object(release, "prepare_release") as prepare,
        patch.object(release, "git", wraps=release.git) as git,
    ):
        release.main()
    assert len(ensure.call_args_list) == 2
    assert all(call.args == (checkout, cli_source) for call in ensure.call_args_list)
    assert build.call_args.args[1] == expected
    prepare.assert_called_once_with(checkout, tmp_path / "sdk.whl", expected)
    assert any(
        call.args == (checkout, "pull", "--ff-only", "origin", release.PUBLIC_BRANCH)
        for call in git.call_args_list
    )
    assert all(call.args[1] not in {"push", "commit", "add", "clone"} for call in git.call_args_list)
    assert checkout.exists()
    output = capsys.readouterr().out
    assert f"Prepared RLE SDK {expected}. Registry latest updated locally to {expected}. Nothing pushed." in output
    assert f"Public registry (after push): {release.MANIFEST_URL}" in output
    assert f"PR: {release.PUBLIC_PR_URL}" in output


@pytest.mark.parametrize("failure", [
    "ensure_public_checkout", "build_wheel", "prepare_release",
])
def test_user_checkout_is_retained_when_release_fails(
    tmp_path, monkeypatch, cli_source, capsys, failure,
):
    checkout = tmp_path / "public"
    checkout.mkdir()
    marker = checkout / "retained.txt"
    marker.write_text("Keep this checkout.\n")
    monkeypatch.setattr(sys, "argv", ["release.py", "--public-checkout", str(checkout)])
    with (
        patch.object(release, "ensure_public_checkout") as ensure,
        patch.object(release, "build_wheel", return_value=tmp_path / "sdk.whl") as build,
        patch.object(release, "prepare_release") as prepare,
    ):
        {"ensure_public_checkout": ensure, "build_wheel": build,
         "prepare_release": prepare}[failure].side_effect = ValueError("Release failed")
        with pytest.raises(ValueError, match="Release failed"):
            release.main()
    assert marker.read_text() == "Keep this checkout.\n"
    assert "Prepared RLE SDK" not in capsys.readouterr().out


@pytest.mark.parametrize("option", ["--publish", "--publish-only", "--github-user"])
def test_cli_rejects_removed_workflows(tmp_path, monkeypatch, option):
    monkeypatch.setattr(sys, "argv", [
        "release.py", "--public-checkout", str(tmp_path / "public"), option,
    ])
    with patch.object(release, "build_wheel") as build:
        with pytest.raises(SystemExit) as error:
            release.main()
    assert error.value.code == 2
    build.assert_not_called()


def test_cli_requires_an_explicit_public_checkout(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["release.py"])
    with patch.object(release, "git") as git:
        with pytest.raises(SystemExit) as error:
            release.main()
    assert error.value.code == 2
    git.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"version": SECOND_VERSION}, {"extra": "voice"}, {"module": False},
])
def test_wrong_wheel_contents_fail(tmp_path, kwargs):
    wheel = tmp_path / "sdk.whl"
    wheel.write_bytes(wheel_bytes(**kwargs))
    with pytest.raises(ValueError):
        release.validate_wheel(wheel, VERSION)


@pytest.mark.parametrize("first_version", [VERSION, LEGACY_VERSION])
def test_releases_preserve_versions_and_leave_changes_unstaged(tmp_path, first_version):
    checkout = public_checkout(tmp_path)
    source = tmp_path / "private-source"
    source.mkdir()
    release.ensure_public_checkout(checkout, source)
    original_head = release.git(checkout, "rev-parse", "HEAD")
    first_wheel = tmp_path / f"azure_ai_projects-{first_version}-py3-none-any.whl"
    first_wheel.write_bytes(wheel_bytes(first_version))
    first = release.prepare_release(checkout, first_wheel, first_version)
    assert first["channel"] == "rle"
    previous = first["versions"][first_version].copy()
    assert previous["url"].split("/")[5] == release.PUBLIC_BRANCH
    path = f"{release.PUBLIC_PATH}/wheels/{first_version}/{first_wheel.name}"
    assert (checkout / path).read_bytes() == first_wheel.read_bytes()
    assert release.git(checkout, "rev-parse", "HEAD") == original_head
    assert release.git(checkout, "diff", "--cached", "--name-only") == ""
    release.git(checkout, "add", "--", release.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "User commits the reviewed release")
    committed_head = release.git(checkout, "rev-parse", "HEAD")
    second_wheel = tmp_path / f"azure_ai_projects-{SECOND_VERSION}-py3-none-any.whl"
    second_wheel.write_bytes(wheel_bytes(SECOND_VERSION))
    second = release.prepare_release(checkout, second_wheel, SECOND_VERSION)
    assert second["versions"][first_version] == previous
    assert second["latest"] == SECOND_VERSION
    assert set(second["versions"]) == {first_version, SECOND_VERSION}
    assert set(release.public_changes(checkout)) == {
        f"{release.PUBLIC_PATH}/registry.json",
        f"{release.PUBLIC_PATH}/wheels/{first_version}/{first_wheel.name}",
    }
    assert release.git(checkout, "rev-parse", "HEAD") == committed_head
    assert release.git(checkout, "diff", "--cached", "--name-only") == ""
    assert release.git(checkout, "diff", "--name-only") == f"{release.PUBLIC_PATH}/registry.json"
    assert release.git(checkout, "ls-files", "--others", "--exclude-standard") == (
        f"{release.PUBLIC_PATH}/wheels/{SECOND_VERSION}/{second_wheel.name}"
    )
    with pytest.raises(ValueError, match="already released"):
        release.prepare_release(checkout, first_wheel, first_version)
    assert release.git(checkout, "rev-parse", "HEAD") == committed_head


@pytest.mark.parametrize("path", [
    "sdk/ai/azure-ai-projects/azure/ai/projects/rle/__init__.py",
    f"{release.PUBLIC_PATH}/release.py",
    f"{release.PUBLIC_PATH}/install_rle_sdk.py",
    f"{release.PUBLIC_PATH}/README.md",
    f"{release.PUBLIC_PATH}/wheels/{VERSION}/secret.txt",
    "sdk/ai/azure-ai-projects/rle-external/registry.json",
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


def test_missing_checkout_is_rejected_without_cloning(tmp_path):
    checkout = tmp_path / "missing-public"
    with patch.object(release, "git") as git:
        with pytest.raises(ValueError, match="existing public SDK checkout"):
            release.ensure_public_checkout(checkout, tmp_path / "source")
    git.assert_not_called()
    assert not checkout.exists()


@pytest.mark.parametrize("remote", [
    f"https://github.com/{release.PUBLIC_REPOSITORY}.git",
    f"git@github.com:{release.PUBLIC_REPOSITORY}.git",
    f"ssh://git@github.com/{release.PUBLIC_REPOSITORY}.git",
])
def test_existing_checkout_accepts_standard_public_remotes(tmp_path, remote):
    checkout = tmp_path / "public"
    checkout.mkdir()
    with patch.object(release, "git") as git:
        git.side_effect = [str(checkout), remote, release.PUBLIC_BRANCH, "", "", "a" * 40, ""]
        release.ensure_public_checkout(checkout, tmp_path / "source")
    assert all(call.args[1] not in {"clone", "switch", "reset", "push"} for call in git.call_args_list)


@pytest.mark.parametrize("branch", ["main", "", "other"])
def test_wrong_or_detached_branch_is_rejected(tmp_path, branch):
    checkout = public_checkout(tmp_path)
    if branch:
        release.git(checkout, "switch", "-C", branch)
    else:
        release.git(checkout, "switch", "--detach")
    with pytest.raises(ValueError, match="must use branch"):
        release.ensure_public_checkout(checkout, tmp_path / "source")


def test_wrong_repository_is_rejected(tmp_path):
    checkout = public_checkout(tmp_path)
    release.git(checkout, "remote", "set-url", "origin", "https://github.com/example/private.git")
    with pytest.raises(ValueError, match="upstream SDK repository"):
        release.ensure_public_checkout(checkout, tmp_path / "source")


def test_failed_fast_forward_pull_stops_before_building(tmp_path, monkeypatch, cli_source):
    checkout = tmp_path / "public"
    checkout.mkdir()
    monkeypatch.setattr(sys, "argv", ["release.py", "--public-checkout", str(checkout)])
    original_git = release.git

    def git(root, *args):
        if args[0] == "pull":
            raise subprocess.CalledProcessError(128, ["git", "pull", "--ff-only"])
        return original_git(root, *args)

    with patch.object(release, "git", side_effect=git):
        with patch.object(release, "build_wheel") as build:
            with pytest.raises(subprocess.CalledProcessError):
                release.main()
    build.assert_not_called()
    assert checkout.exists()


def test_real_pull_refreshes_registry_and_leaves_only_unstaged_release_files(
    tmp_path, monkeypatch, capsys,
):
    upstream = public_checkout(tmp_path)
    remote = tmp_path / "remote.git"
    release.git(tmp_path, "clone", "--bare", str(upstream), str(remote))
    checkout = tmp_path / "user checkout"
    release.git(tmp_path, "clone", "--branch", release.PUBLIC_BRANCH, str(remote), str(checkout))
    source = tmp_path / "source"
    source.mkdir()
    package = source / "package"
    version_file = package / "azure" / "ai" / "projects" / "_version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('VERSION = "2.7.0"\n')
    release.git(source, "init", "--initial-branch=main")
    release.git(source, "config", "user.name", "Release test")
    release.git(source, "config", "user.email", "test@example.invalid")
    release.git(source, "add", "--all")
    release.git(source, "commit", "-m", "Committed SDK source")
    registry = upstream / release.PUBLIC_PATH / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps(manifest_for("2.7.2b1")))
    release.git(upstream, "add", "--", release.PUBLIC_PATH)
    release.git(upstream, "commit", "-m", "Newer approved SDK")
    release.git(upstream, "push", str(remote), release.PUBLIC_BRANCH)
    remote_head = release.git(remote, f"--git-dir={remote}", "rev-parse", release.PUBLIC_BRANCH)
    monkeypatch.setattr(release, "PACKAGE", package)
    monkeypatch.setattr(sys, "argv", ["release.py", "--public-checkout", str(checkout)])
    real_git = release.git

    def git(root, *args):
        if root == checkout and args == ("remote", "get-url", "origin"):
            return f"https://github.com/{release.PUBLIC_REPOSITORY}.git"
        return real_git(root, *args)

    def build(package, version, workspace):
        assert version == "2.7.3b1"
        wheel = workspace / f"azure_ai_projects-{version}-py3-none-any.whl"
        wheel.write_bytes(wheel_bytes(version))
        return wheel

    with patch.object(release, "git", side_effect=git):
        with patch.object(release, "build_wheel", side_effect=build):
            release.main()
    assert real_git(checkout, "rev-parse", "HEAD") == remote_head
    assert real_git(remote, f"--git-dir={remote}", "rev-parse", release.PUBLIC_BRANCH) == remote_head
    assert real_git(checkout, "diff", "--cached", "--name-only") == ""
    assert real_git(checkout, "diff", "--name-only") == f"{release.PUBLIC_PATH}/registry.json"
    assert real_git(checkout, "ls-files", "--others", "--exclude-standard") == (
        f"{release.PUBLIC_PATH}/wheels/2.7.3b1/azure_ai_projects-2.7.3b1-py3-none-any.whl"
    )
    manifest = release.read_registry(checkout)
    assert manifest["latest"] == "2.7.3b1"
    assert "2.7.2b1" in manifest["versions"]
    assert version_file.read_text() == 'VERSION = "2.7.0"\n'
    assert "Nothing pushed." in capsys.readouterr().out


def test_shallow_checkout_deepens_history_to_find_the_pr_base(tmp_path):
    changed = f"{release.PUBLIC_PATH}/registry.json"
    with patch.object(release, "git") as git:
        git.side_effect = [
            "",
            subprocess.CalledProcessError(1, ["git", "merge-base"]),
            "", "a" * 40, changed,
        ]
        assert release.public_changes(tmp_path) == [changed]
    assert git.call_args_list[0].args == (
        tmp_path, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main",
    )
    assert git.call_args_list[2].args == (
        tmp_path, "fetch", "--deepen=50", "origin", "main", release.PUBLIC_BRANCH,
    )
    assert git.call_args_list[-1].args == (
        tmp_path, "diff", "--name-only", "origin/main...HEAD",
    )


def test_pr_base_lookup_does_not_hide_git_errors(tmp_path):
    with patch.object(release, "git") as git:
        git.side_effect = ["", subprocess.CalledProcessError(128, ["git", "merge-base"])]
        with pytest.raises(subprocess.CalledProcessError):
            release.public_changes(tmp_path)
    assert git.call_count == 2


def test_main_fetch_failure_stops_public_history_validation(tmp_path):
    with patch.object(release, "git", side_effect=subprocess.CalledProcessError(128, ["git", "fetch"])) as git:
        with pytest.raises(subprocess.CalledProcessError):
            release.public_changes(tmp_path)
    git.assert_called_once()


def test_refreshing_stale_main_accepts_legitimate_public_base_merge(tmp_path):
    checkout = public_checkout(tmp_path)
    original_main = release.git(checkout, "rev-parse", "origin/main")
    release.git(checkout, "switch", "main")
    (checkout / "baseline.txt").write_text("New upstream SDK source.\n")
    release.git(checkout, "add", "baseline.txt")
    release.git(checkout, "commit", "-m", "Advance public main")
    release.git(checkout, "switch", release.PUBLIC_BRANCH)
    release.git(checkout, "merge", "--no-edit", "main")
    original_head = release.git(checkout, "rev-parse", "HEAD")
    assert release.git(checkout, "rev-parse", "origin/main") == original_main
    release.ensure_public_checkout(checkout, tmp_path / "source")
    assert release.git(checkout, "rev-parse", "origin/main") == release.git(checkout, "rev-parse", "main")
    assert release.public_changes(checkout) == []
    assert release.git(checkout, "rev-parse", "HEAD") == original_head
    assert release.git(checkout, "status", "--porcelain") == ""


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
    release.git(checkout, "add", "--", release.PUBLIC_PATH)
    release.git(checkout, "commit", "-m", "User commits the reviewed release")
    release.ensure_public_checkout(checkout, tmp_path / "private-source")
    assert set(release.public_changes(checkout)) == {
        f"{release.PUBLIC_PATH}/registry.json",
        f"{release.PUBLIC_PATH}/wheels/{VERSION}/{wheel.name}",
    }


@pytest.mark.parametrize("existing_main_ref", [False, True])
def test_real_shallow_clone_deepens_and_temporary_git_files_are_cleaned_up(tmp_path, existing_main_ref):
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
            tmp_path, "clone", "--depth=1", "--single-branch", "--branch", release.PUBLIC_BRANCH,
            upstream.as_uri(), str(checkout),
        )
        if existing_main_ref:
            release.git(checkout, "fetch", "--depth=1", "origin", "main:refs/remotes/origin/main")
            with pytest.raises(subprocess.CalledProcessError):
                release.git(checkout, "merge-base", "origin/main", "HEAD")
        else:
            assert release.git(checkout, "for-each-ref", "refs/remotes/origin/main") == ""
        original_head = release.git(checkout, "rev-parse", "HEAD")
        assert release.public_changes(checkout) == [registry_path]
        release.validate_public_paths(release.public_changes(checkout))
        assert release.git(checkout, "rev-parse", "HEAD") == original_head
        assert release.git(checkout, "status", "--porcelain") == ""
    assert not Path(temporary).exists()


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
    artifacts = package / "rle"
    artifacts.mkdir()
    (artifacts / "registry.json").write_text("{}")
    runtime_module = version_file.parent / "rle" / "environments" / "__init__.py"
    runtime_module.parent.mkdir(parents=True)
    runtime_module.write_text("runtime RLE module\n")
    workspace = tmp_path / "build"
    workspace.mkdir()

    def build(command, **kwargs):
        staged = workspace / "package"
        assert (staged / version_file.relative_to(package)).read_text() == f'VERSION = "{VERSION}"\n'
        assert not (staged / "scripts").exists()
        assert not (staged / "rle").exists()
        assert (staged / runtime_module.relative_to(package)).read_text() == "runtime RLE module\n"
        wheels = workspace / "wheels"
        wheels.mkdir()
        (wheels / f"azure_ai_projects-{VERSION}-py3-none-any.whl").write_bytes(wheel_bytes())

    with patch.object(release.subprocess, "run", side_effect=build):
        wheel = release.build_wheel(package, VERSION, workspace)
    assert wheel.exists()
    assert version_file.read_text() == 'VERSION = "2.7.0"\n'
