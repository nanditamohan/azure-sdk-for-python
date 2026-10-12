# External RLE SDK preview distribution

The public distribution contains only packaged `azure-ai-projects` wheels and a
[`registry.json`](https://raw.githubusercontent.com/Azure/azure-sdk-for-python/rle-external-releases/sdk/ai/azure-ai-projects/rle-external/registry.json)
manifest. Release tooling, its validation helpers and this documentation stay in the
source fork; consumer installers live only in the RLE samples. Reading
the public manifest and downloading wheels requires no source repository
access or credentials. Wheels contain Python code: publishing them is public
distribution of the implementation, not a way to keep shipped code confidential.

## Install

Run the standalone installer included in your RLE sample's Docker build directory:

```powershell
python install_rle_sdk.py
python install_rle_sdk.py --version 2.7.0b1
```

The example version is illustrative; use a version listed in the registry.
For local Python installation, `--version` can override the installer's
`AZURE_AI_PROJECTS_RLE_VERSION` environment value; otherwise it defaults to
`latest`. An empty, invalid, or unavailable explicit version fails. The
installer verifies the wheel's SHA-256, package version, RLE extra and environment
module before pip installs the wheel with its `rle` dependencies. Dependencies
use the normal pip index or `--index-url`; the SDK itself never resolves through
that index. `--requirements` installs other requirements in the same transaction.

## Manifest contract

The manifest is a JSON object with `schema_version: 1`, `package:
"azure-ai-projects"`, `channel: "rle-external"`, `latest`, and `versions`. Each
version maps to a wheel `filename`, immutable commit-pinned public `url`, and
lowercase hexadecimal `sha256`. New universal `py3-none-any` wheels use unique
`MAJOR.MINOR.PATCHbNUMBER` beta versions. Previously published timestamped `.dev`
versions remain valid and their artifacts and registry entries are preserved.
`latest` means the most
recent deliberately published RLE build, not the latest package on PyPI.
Existing version entries and artifacts must never be overwritten.

## Container builds

Use Git-source Docker `ADD` of the public distribution subdirectory before SDK
installation, with a Git-source-ADD-capable builder such as Docker with BuildKit:

```dockerfile
ADD https://github.com/Azure/azure-sdk-for-python.git#rle-external-releases:sdk/ai/azure-ai-projects/rle-external /app/rle-sdk-dist/
```

The builder resolves the public branch commit and scopes the image contents to
this subdirectory; no Git installation is needed in the runtime image. New
release commits invalidate the SDK installation layer. Raw GitHub HTTP manifests
have a five-minute CDN cache and GitHub REST has anonymous rate limits, so neither
is a reliable Docker cache-busting input. `pip --no-cache-dir` alone does not
invalidate Docker layers. Do not bake a registry snapshot into the sample.

Sample Dockerfiles set the build-time SDK selection directly before installation:

```dockerfile
ENV AZURE_AI_PROJECTS_RLE_VERSION=latest
```

Consumers can edit that line in their local Dockerfile to pin a version from the
registry. No host SDK environment variable, Docker build argument, version file,
or `azd` CLI configuration is needed. The CLI builds the container; it neither
installs nor selects the SDK. Runtime container variables cannot replace the SDK
already baked into an image.

## Maintainer release process

Release tooling lives in the source checkout at
`sdk/ai/azure-ai-projects/scripts/rle-external/release.py`, not in this public
distribution. Install the `build` package in a dedicated maintainer environment.
Commit SDK implementation changes before building.

```powershell
python sdk\ai\azure-ai-projects\scripts\rle-external\release.py `
  --public-checkout C:\work\azure-sdk-for-python-rle-external

# After inspecting the prepared artifacts:
python sdk\ai\azure-ai-projects\scripts\rle-external\release.py `
  --public-checkout C:\work\azure-sdk-for-python-rle-external --publish-only
```

Use `--publish` to build and publish in one invocation, and `--version` to choose
an explicit unique beta version, such as `2.7.0b1`. Otherwise the script selects
the next beta number for the SDK base version from the public checkout's registry:
`2.7.0b1`, `2.7.0b2`, and so on. Refresh an existing public checkout before
preparing a release so numbering includes all published versions. Python
normalizes literal `preview` version labels to release candidates, so beta
notation is used instead. New `.dev` releases are no longer accepted by the CLI.
The source package version is never modified in place. Validate the prepared wheel
and sample builds before publishing; only deliberately approved releases should
advance `latest`.

The public checkout is separate from the source checkout and based on upstream
`main`. First use creates it automatically. Only wheel artifacts and the JSON
manifest may differ from upstream; the release guard rejects scripts and
documentation in the public distribution. Artifact and manifest
updates use separate commits so manifest URLs pin immutable artifact commits.
Publishing uses `GH_TOKEN`, `GITHUB_TOKEN`, or Git's GitHub credentials; it requires
write permission on the upstream distribution branch and permission to open PRs.
No credentials are written into repository files or remote URLs.
If Git Credential Manager has multiple accounts, pass `--github-user <account>`
to select the existing GitHub login without changing global Git settings.

Publishing verifies the manifest and every retained wheel anonymously, then
creates or reuses a draft PR. If upload or verification fails, the command fails;
retry the prepared release with `--publish-only`. Keep the branch available even
if the PR is closed. Deleting it breaks latest discovery.
