"""Build RLE SDK wheels and prepare or publish the public distribution PR."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
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

from install_rle_sdk import (
    MANIFEST_URL, PUBLIC_BRANCH, PUBLIC_PATH, PUBLIC_REPOSITORY,
    VERSION_PATTERN, resolve_release, validate_wheel,
)

PACKAGE = Path(__file__).resolve().parents[2]
TOOLING = Path(__file__).resolve().parent
TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


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
    allowed = {f"{PUBLIC_PATH}/{name}" for name in (
        "registry.json", "install_rle_sdk.py", "README.md",
    )}
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
    if registry_path.exists():
        manifest = json.loads(registry_path.read_text(encoding="utf-8"))
        resolve_release(manifest)
        for prior in manifest["versions"]:
            resolve_release(manifest, prior)
    else:
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
    for name in ("install_rle_sdk.py", "README.md"):
        shutil.copyfile(TOOLING / name, public / name)
    resolve_release(manifest)
    git(checkout, "add", "--", f"{PUBLIC_PATH}/registry.json",
        f"{PUBLIC_PATH}/install_rle_sdk.py", f"{PUBLIC_PATH}/README.md")
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
    from install_rle_sdk import download_wheel

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
        "title": "Distribute external RLE SDK wheels and registry",
        "head": PUBLIC_BRANCH, "base": "main", "draft": True,
        "body": (
            "## External RLE SDK preview distribution\n\n"
            "This long-lived draft PR contains only packaged SDK wheels, a public JSON "
            "manifest, an installer and distribution documentation. No SDK implementation "
            "source changes or private repository references are included.\n\n"
            f"Registry: {MANIFEST_URL}\n\n"
            "Wheel URLs are pinned to artifact commits and verified with SHA-256. "
            "Container builds support latest and exact versions without source access. "
            "Future releases reuse this branch and PR, preserving previous versions. "
            "Please retain the branch while customers use this preview distribution.\n\n"
            "The manifest and all wheel downloads were verified anonymously before PR creation."
        ),
    })
    if not isinstance(pull, dict) or not isinstance(pull.get("html_url"), str):
        raise ValueError("GitHub did not return a PR URL.")
    return pull["html_url"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Unique MAJOR.MINOR.PATCH.devNUMBER package version")
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
        version = args.version or (
            f"{match[1]}.dev{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        )
        if not re.fullmatch(VERSION_PATTERN, version):
            raise ValueError("Use a unique MAJOR.MINOR.PATCH.devNUMBER version.")
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
