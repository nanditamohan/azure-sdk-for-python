"""Prepare SDK beta wheels and registry changes in an existing public checkout."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

PUBLIC_REPOSITORY = "Azure/azure-sdk-for-python"
PUBLIC_BRANCH = "rle-external-releases"
PUBLIC_PATH = "sdk/ai/azure-ai-projects/rle-external"
PUBLIC_PR_URL = f"https://github.com/{PUBLIC_REPOSITORY}/pull/49482"
MANIFEST_URL = (
    f"https://raw.githubusercontent.com/{PUBLIC_REPOSITORY}/"
    f"{PUBLIC_BRANCH}/{PUBLIC_PATH}/registry.json"
)
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:b[1-9][0-9]*|\.dev[0-9]+)"

PACKAGE = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    url: str
    sha256: str


def resolve_release(manifest: object, version: str = "latest") -> Release:
    if not isinstance(manifest, dict):
        raise ValueError("The SDK manifest must be a JSON object.")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("package") != "azure-ai-projects"
        or manifest.get("channel") != "rle-external"
    ):
        raise ValueError("Unsupported SDK manifest schema, package, or channel.")
    selected = manifest.get("latest") if version == "latest" else version
    if not isinstance(selected, str) or not re.fullmatch(VERSION_PATTERN, selected):
        raise ValueError(f"Invalid RLE SDK version: {selected!r}")
    versions = manifest.get("versions")
    if not isinstance(versions, dict) or selected not in versions:
        raise ValueError(f"RLE SDK version {selected!r} is not available.")
    entry = versions[selected]
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid release entry for {selected}.")
    filename = f"azure_ai_projects-{selected}-py3-none-any.whl"
    url = entry.get("url")
    digest = entry.get("sha256")
    if entry.get("filename") != filename:
        raise ValueError(f"Unexpected wheel filename for {selected}.")
    expected_url = (
        rf"https://raw\.githubusercontent\.com/{re.escape(PUBLIC_REPOSITORY)}/"
        rf"(?:[0-9a-f]{{40}}|{re.escape(PUBLIC_BRANCH)})/{re.escape(PUBLIC_PATH)}/wheels/"
        rf"{re.escape(selected)}/{re.escape(filename)}"
    )
    if not isinstance(url, str) or not re.fullmatch(expected_url, url):
        raise ValueError("The wheel URL must reference a public SDK commit or release branch.")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("The wheel SHA-256 must contain 64 lowercase hexadecimal digits.")
    return Release(selected, filename, url, digest)


def validate_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError("Expected exactly one wheel METADATA file.")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        if metadata["Name"] != "azure-ai-projects" or metadata["Version"] != version:
            raise ValueError("Wheel package name or version does not match the manifest.")
        if "rle" not in metadata.get_all("Provides-Extra", []):
            raise ValueError("The wheel does not declare the rle extra.")
        if "azure/ai/projects/rle/environments/__init__.py" not in names:
            raise ValueError("The wheel does not contain RLEnvironment.")
        if archive.testzip() is not None:
            raise ValueError("The wheel contains corrupt entries.")


def read_registry(checkout: Path) -> dict | None:
    path = checkout / PUBLIC_PATH / "registry.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    resolve_release(manifest)
    for version in manifest["versions"]:
        resolve_release(manifest, version)
    return manifest


def next_version(checkout: Path, base_version: str, bump: str = "patch") -> str:
    if bump not in {"patch", "minor", "major"}:
        raise ValueError("Version bump must be patch, minor, or major.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", base_version):
        raise ValueError("The SDK base version must be MAJOR.MINOR.PATCH.")
    manifest = read_registry(checkout)
    versions = [base_version, *(manifest["versions"] if manifest is not None else {})]
    major, minor, patch = max(
        tuple(int(part) for part in re.split(r"b|\.dev", version, maxsplit=1)[0].split("."))
        for version in versions
    )
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}b1"


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments], check=True,
        text=True, stdout=subprocess.PIPE,
    ).stdout.strip()


def ensure_public_checkout(checkout: Path, source: Path) -> None:
    if checkout == source or source in checkout.parents or checkout in source.parents:
        raise ValueError("The public checkout must be separate from the source checkout.")
    if not checkout.is_dir():
        raise ValueError("Provide an existing public SDK checkout with --public-checkout.")
    if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ValueError("The public checkout must be a repository root.")
    origin = git(checkout, "remote", "get-url", "origin")
    if origin.removesuffix(".git") not in {
        f"https://github.com/{PUBLIC_REPOSITORY}",
        f"git@github.com:{PUBLIC_REPOSITORY}",
        f"ssh://git@github.com/{PUBLIC_REPOSITORY}",
    }:
        raise ValueError("The public checkout origin must be the upstream SDK repository.")
    if git(checkout, "branch", "--show-current") != PUBLIC_BRANCH:
        raise ValueError(f"The public checkout must use branch {PUBLIC_BRANCH}.")
    if git(checkout, "status", "--porcelain"):
        raise ValueError("The public checkout must be clean before release preparation.")
    validate_public_paths(public_changes(checkout))


def public_changes(checkout: Path) -> list[str]:
    for deepen in (50, 200, 1000):
        try:
            git(checkout, "merge-base", "origin/main", "HEAD")
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            git(checkout, "fetch", f"--deepen={deepen}", "origin", "main", PUBLIC_BRANCH)
        else:
            break
    return git(checkout, "diff", "--name-only", "origin/main...HEAD").splitlines()


def validate_public_paths(paths: list[str]) -> None:
    allowed = {f"{PUBLIC_PATH}/registry.json"}
    wheel_pattern = (
        rf"{re.escape(PUBLIC_PATH)}/wheels/({VERSION_PATTERN})/"
        rf"azure_ai_projects-\1-py3-none-any\.whl"
    )
    for path in paths:
        if path not in allowed and not re.fullmatch(wheel_pattern, path):
            raise ValueError(f"Refusing to publish a non-distribution file: {path}")


def build_wheel(package: Path, version: str, workspace: Path) -> Path:
    staged = workspace / "package"
    shutil.copytree(
        package, staged,
        ignore=shutil.ignore_patterns(
            "scripts", "rle-external", "build", "dist", "*.egg-info",
            "__pycache__", ".venv", ".env", ".env.*",
        ),
    )
    version_file = staged / "azure" / "ai" / "projects" / "_version.py"
    original = version_file.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^VERSION = "[^"]+"$', f'VERSION = "{version}"', original, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError("Expected exactly one SDK VERSION assignment.")
    version_file.write_text(updated, encoding="utf-8")
    destination = workspace / "wheels"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(destination), str(staged)],
        check=True,
    )
    wheels = list(destination.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].name != f"azure_ai_projects-{version}-py3-none-any.whl":
        raise ValueError("Expected one versioned, platform-independent SDK wheel.")
    validate_wheel(wheels[0], version)
    return wheels[0]


def prepare_release(checkout: Path, wheel: Path, version: str) -> dict:
    public = checkout / PUBLIC_PATH
    registry_path = public / "registry.json"
    manifest = read_registry(checkout)
    if manifest is None:
        manifest = {
            "schema_version": 1, "package": "azure-ai-projects",
            "channel": "rle-external", "latest": version, "versions": {},
        }
    if version in manifest["versions"]:
        raise ValueError(f"SDK {version} is already released; use a new version.")
    target = public / "wheels" / version / wheel.name
    if target.exists():
        raise ValueError(f"An artifact already exists for SDK {version}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(wheel, target)
    relative = target.relative_to(checkout).as_posix()
    manifest["versions"][version] = {
        "filename": wheel.name,
        "url": f"https://raw.githubusercontent.com/{PUBLIC_REPOSITORY}/{PUBLIC_BRANCH}/{relative}",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    manifest["latest"] = version
    registry_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    resolve_release(manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-checkout", type=Path, required=True)
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    args = parser.parse_args()
    source = Path(git(PACKAGE, "rev-parse", "--show-toplevel")).resolve()
    checkout = args.public_checkout.resolve()
    ensure_public_checkout(checkout, source)
    if git(
        source, "status", "--porcelain", "--untracked-files=all", "--", str(PACKAGE),
        ":(exclude)sdk/ai/azure-ai-projects/scripts/rle",
    ):
        raise ValueError("Commit SDK package changes before building a release.")
    print(f"Refreshing public branch {PUBLIC_BRANCH}...", flush=True)
    git(checkout, "pull", "--ff-only", "origin", PUBLIC_BRANCH)
    ensure_public_checkout(checkout, source)
    current = (PACKAGE / "azure" / "ai" / "projects" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)[^"]*"$', current, re.MULTILINE)
    if match is None:
        raise ValueError("Cannot determine the SDK base version.")
    version = next_version(checkout, match[1], args.bump)
    print(f"Building RLE SDK {version} ({args.bump} bump)...", flush=True)
    with tempfile.TemporaryDirectory(prefix="rle-sdk-build-") as temporary:
        wheel = build_wheel(PACKAGE, version, Path(temporary))
        prepare_release(checkout, wheel, version)
    print(f"Prepared RLE SDK {version}. Registry latest updated locally to {version}. Nothing pushed.")
    print(f"Unstaged changes: {checkout / PUBLIC_PATH}")
    print("Review/test the wheel, then commit and push both the wheel and registry manually.")
    print(f"Public registry (after push): {MANIFEST_URL}")
    print(f"PR: {PUBLIC_PR_URL}")


if __name__ == "__main__":
    main()
