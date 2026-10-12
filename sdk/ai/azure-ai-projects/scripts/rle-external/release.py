"""Build RLE SDK wheels and prepare or publish the public distribution PR."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile

PUBLIC_REPOSITORY = "Azure/azure-sdk-for-python"
PUBLIC_BRANCH = "rle-external-releases"
PUBLIC_PATH = "sdk/ai/azure-ai-projects/rle-external"
MANIFEST_URL = (
    f"https://raw.githubusercontent.com/{PUBLIC_REPOSITORY}/"
    f"{PUBLIC_BRANCH}/{PUBLIC_PATH}/registry.json"
)
BETA_VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+b[1-9][0-9]*"
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:b[1-9][0-9]*|\.dev[0-9]+)"

PACKAGE = Path(__file__).resolve().parents[2]
TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


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
        rf"[0-9a-f]{{40}}/{re.escape(PUBLIC_PATH)}/wheels/"
        rf"{re.escape(selected)}/{re.escape(filename)}"
    )
    if not isinstance(url, str) or not re.fullmatch(expected_url, url):
        raise ValueError("The wheel URL must reference an immutable public SDK commit.")
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


def download_wheel(release: Release, directory: Path) -> Path:
    target = directory / release.filename
    digest = hashlib.sha256()
    with urlopen(release.url, timeout=120) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != release.sha256:
        target.unlink()
        raise ValueError(f"SHA-256 mismatch for SDK {release.version}; refusing publication.")
    validate_wheel(target, release.version)
    return target


def read_registry(checkout: Path) -> dict | None:
    path = checkout / PUBLIC_PATH / "registry.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    resolve_release(manifest)
    for version in manifest["versions"]:
        resolve_release(manifest, version)
    return manifest


def next_beta_version(checkout: Path, base_version: str) -> str:
    manifest = read_registry(checkout)
    numbers = [
        int(match[1])
        for version in (manifest["versions"] if manifest is not None else {})
        if (match := re.fullmatch(rf"{re.escape(base_version)}b([1-9][0-9]*)", version))
    ]
    return f"{base_version}b{max(numbers, default=0) + 1}"


def git(checkout: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments], check=True,
        text=True, stdout=subprocess.PIPE, env=env,
    ).stdout.strip()


def ensure_public_checkout(checkout: Path, source: Path) -> None:
    if checkout == source or source in checkout.parents or checkout in source.parents:
        raise ValueError("The public checkout must be separate from the source checkout.")
    if not checkout.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
             f"https://github.com/{PUBLIC_REPOSITORY}.git", str(checkout)],
            check=True,
        )
        git(checkout, "sparse-checkout", "set", PUBLIC_PATH)
        remote_branch = git(
            checkout, "ls-remote", "--heads", "origin", f"refs/heads/{PUBLIC_BRANCH}"
        )
        if remote_branch:
            git(checkout, "fetch", "--depth=1", "origin", PUBLIC_BRANCH)
            git(checkout, "switch", "-c", PUBLIC_BRANCH, "FETCH_HEAD")
        else:
            git(checkout, "switch", "-c", PUBLIC_BRANCH)
    if Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
        raise ValueError("The public checkout must be a repository root.")
    origin = git(checkout, "remote", "get-url", "origin")
    if origin.removesuffix(".git") != f"https://github.com/{PUBLIC_REPOSITORY}":
        raise ValueError("The public checkout origin must be the upstream SDK repository.")
    if git(checkout, "branch", "--show-current") != PUBLIC_BRANCH:
        raise ValueError(f"The public checkout must use branch {PUBLIC_BRANCH}.")
    if git(checkout, "status", "--porcelain"):
        raise ValueError("The public checkout must be clean before release preparation.")
    changed = git(checkout, "diff", "--name-only", "origin/main", "HEAD").splitlines()
    validate_public_paths(changed)


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
    git(checkout, "add", "--", relative)
    git(checkout, "commit", "-m", f"Package RLE SDK {version}\n\n{TRAILER}")
    artifact_commit = git(checkout, "rev-parse", "HEAD")
    manifest["versions"][version] = {
        "filename": wheel.name,
        "url": f"https://raw.githubusercontent.com/{PUBLIC_REPOSITORY}/{artifact_commit}/{relative}",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    manifest["latest"] = version
    registry_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    resolve_release(manifest)
    git(checkout, "add", "--", f"{PUBLIC_PATH}/registry.json")
    git(checkout, "commit", "-m", f"Distribute RLE SDK {version}\n\n{TRAILER}")
    return manifest


def github_token(username: str | None = None) -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    credential_request = "protocol=https\nhost=github.com\n"
    if username is not None:
        if not re.fullmatch(r"[A-Za-z0-9-]+", username):
            raise ValueError("Invalid GitHub username.")
        credential_request += f"username={username}\n"
    result = subprocess.run(
        ["git", "credential", "fill"], input=credential_request + "\n",
        text=True, capture_output=True,
    )
    credentials = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if result.returncode or not credentials.get("password"):
        raise RuntimeError(
            "Publishing requires GH_TOKEN, GITHUB_TOKEN, or GitHub Git credentials. "
            "Use --github-user to select an account when Git has multiple accounts."
        )
    return credentials["password"]


def github_api(token: str, path: str, data: dict | None = None) -> object:
    request = Request(
        f"https://api.github.com/{path}",
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def publish(checkout: Path, token: str) -> str:
    validate_public_paths(
        git(checkout, "diff", "--name-only", "origin/main", "HEAD").splitlines()
    )
    if git(checkout, "status", "--porcelain"):
        raise ValueError("Refusing to publish a dirty public checkout.")
    manifest = json.loads((checkout / PUBLIC_PATH / "registry.json").read_text(encoding="utf-8"))
    resolve_release(manifest)
    # Keep credentials out of subprocess arguments and the repository configuration.
    env = os.environ.copy()
    index = int(env.get("GIT_CONFIG_COUNT", "0"))
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = str(index + 1)
    env[f"GIT_CONFIG_KEY_{index}"] = "http.https://github.com/.extraheader"
    env[f"GIT_CONFIG_VALUE_{index}"] = f"Authorization: Basic {authorization}"
    git(checkout, "push", "origin", f"HEAD:refs/heads/{PUBLIC_BRANCH}", env=env)
    with urlopen(MANIFEST_URL, timeout=60) as response:
        published = json.load(response)
    if published != manifest:
        raise ValueError("The anonymously downloaded public manifest differs from the release.")
    with tempfile.TemporaryDirectory(prefix="rle-public-check-") as temporary:
        for version in published["versions"]:
            download_wheel(resolve_release(published, version), Path(temporary))
    pulls = github_api(
        token, f"repos/{PUBLIC_REPOSITORY}/pulls?state=open&head=Azure:{PUBLIC_BRANCH}&base=main"
    )
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise ValueError("Expected at most one open SDK distribution PR.")
    if pulls:
        return pulls[0]["html_url"]
    pull = github_api(token, f"repos/{PUBLIC_REPOSITORY}/pulls", {
        "title": "RLE SDK beta preview: wheels and registry",
        "head": PUBLIC_BRANCH, "base": "main", "draft": True,
        "body": f"""## RLE SDK beta preview

Beta release of `azure-ai-projects[rle]` for trying the RLE environment samples.

- [Versions and wheel downloads]({MANIFEST_URL})
- [RLE sample setup](https://github.com/sujit-kamireddy/rle-samples/blob/rle-public-sdk-wheels/README.md#rle-sdk-installation)
""",
    })
    if not isinstance(pull, dict) or not isinstance(pull.get("html_url"), str):
        raise ValueError("GitHub did not return a PR URL.")
    return pull["html_url"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Unique MAJOR.MINOR.PATCHbNUMBER beta version")
    parser.add_argument("--public-checkout", type=Path, required=True)
    parser.add_argument("--github-user", help="Select an existing Git Credential Manager account")
    parser.add_argument("--publish", action="store_true", help="Push artifacts and create/reuse the draft PR")
    parser.add_argument("--publish-only", action="store_true", help="Publish an already prepared clean checkout")
    args = parser.parse_args()
    token = github_token(args.github_user) if args.publish or args.publish_only else None
    source = Path(git(PACKAGE, "rev-parse", "--show-toplevel")).resolve()
    checkout = args.public_checkout.resolve()
    ensure_public_checkout(checkout, source)
    if not args.publish_only:
        if git(
            source, "status", "--porcelain", "--untracked-files=all", "--", str(PACKAGE),
            ":(exclude)sdk/ai/azure-ai-projects/scripts/rle-external",
        ):
            raise ValueError("Commit SDK package changes before building a release.")
        current = (PACKAGE / "azure" / "ai" / "projects" / "_version.py").read_text(encoding="utf-8")
        match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)[^"]*"$', current, re.MULTILINE)
        if match is None:
            raise ValueError("Cannot determine the SDK base version.")
        version = (
            args.version if args.version is not None else next_beta_version(checkout, match[1])
        )
        if not re.fullmatch(BETA_VERSION_PATTERN, version):
            raise ValueError("Use a unique MAJOR.MINOR.PATCHbNUMBER beta version, such as 2.7.0b1.")
        with tempfile.TemporaryDirectory(prefix="rle-sdk-build-") as temporary:
            wheel = build_wheel(PACKAGE, version, Path(temporary))
            prepare_release(checkout, wheel, version)
        print(f"Prepared SDK {version} in {checkout}")
    if token is not None:
        print(publish(checkout, token))
    else:
        print("Not uploaded. Run again with --publish-only to push and create/reuse the draft PR.")


if __name__ == "__main__":
    main()
