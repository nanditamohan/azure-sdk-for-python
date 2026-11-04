# Queued V3 development wheel

This is a uniquely identified **development-only** build, not an official SDK
release or a PyPI publication. It does not replace the existing 2.7.0b7 artifact
or update an official release registry. Only public SDK source is packaged.

Source: [c67546019a11c65e86a0fcb2048930d7a5b1ef31](https://github.com/sujit-kamireddy/azure-sdk-for-python/commit/c67546019a11c65e86a0fcb2048930d7a5b1ef31).
The [provenance record](provenance.json) records the verified baseline, build and
validation evidence. SHA-256:

```text
1cca2b75a5f2fbfa864584bf7f0f830098bf4fe13519cc4e1a39b3bd60a9ea8e
```

Consumers must pin the wheel URL at a full **artifact commit SHA**, not a mutable
branch, and append `#sha256=` with the digest above. Verify the digest before
installation. Training requires rebuilding and selecting an image containing the
pin; changing a recipe alone does not update an existing image. No service
deployment, cloud/model run or live qualification is implied.

## Private-repository delivery

The configured repository is private. Anonymous raw URLs return 404 and are not
a working image-build installation route. Its visibility and access controls
were not changed. The immutable wheel is available to authorized readers at
artifact commit `6a51573691d53e6b7ac8d115031fdd17dc508838`; the authenticated GitHub
Contents API download was verified against the digest above.

Use an **existing authorized prebuild credential route** to stage the wheel and
verify its hash before Docker builds. Then `COPY` and install the staged wheel.
Never put GitHub credentials into Docker `ARG`, `ENV`, layers, URLs, build logs,
or this repository. Do not retain or pin temporary signed download URLs. If the
build does not already have authorized repository-read access, enabling that
route is a separate integration prerequisite, not a reason to use an unverified
anonymous URL or silently fall back to another SDK.

For example, with GitHub CLI already authenticated through the existing approved
route, run this outside the image build and inside a fresh staging directory:

```powershell
@'
import base64
import hashlib
import json
import pathlib
import subprocess

endpoint = (
    "repos/sujit-kamireddy/azure-sdk-for-python/contents/"
    "sdk/ai/azure-ai-projects/rle/wheels/2.7.0b7.dev20261104%2Bqueuedv3.1/"
    "azure_ai_projects-2.7.0b7.dev20261104%2Bqueuedv3.1-py3-none-any.whl"
    "?ref=6a51573691d53e6b7ac8d115031fdd17dc508838"
)
response = subprocess.run(
    ["gh", "api", endpoint], capture_output=True, check=True,
)
record = json.loads(response.stdout)
if record.get("encoding") != "base64":
    raise ValueError("Expected inline wheel content.")
wheel = base64.b64decode(record["content"])
digest = hashlib.sha256(wheel).hexdigest()
expected = "1cca2b75a5f2fbfa864584bf7f0f830098bf4fe13519cc4e1a39b3bd60a9ea8e"
if digest != expected:
    raise ValueError("SDK wheel digest mismatch.")
pathlib.Path(
    "azure_ai_projects-2.7.0b7.dev20261104+queuedv3.1-py3-none-any.whl",
).write_bytes(wheel)
print("Verified SDK development wheel:", digest)
'@ | python -
```

## Public opt-in

```python
async with project_client.rle.get_rollout_client(
    environment_name=environment_name,
    environment_version=environment_version,
    job_id=job_id,
    protocol_version="v3",
    finetune_credential=finetune_credential,
) as session:
    result = await session.execute_rollout(task=task, policy=policy)
```

`protocol_version` defaults to `None`, preserving legacy v2/v1 behavior. V3 offers
only `rle.execute-rollout.v3` and never falls back. The separate optional
fine-tuning credential retains the baseline b7 identity behavior.

## Reproduce the build

Use Python 3.10 or later with `build`, a clean checkout of the source commit
above, and a new scratch output directory. The existing RLE build helper excludes
environment files, scripts and previously published wheel artifacts. Its
`validate_wheel` check verifies package identity, version, RLE contents and ZIP
integrity. The helper is reused directly because its release command targets the
official release repository/branch and intentionally is not used for this fork's
development publication.

From the repository root in PowerShell:

```powershell
$env:SOURCE_DATE_EPOCH = "1793831274"
@'
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "rle_release",
    pathlib.Path(r"sdk\ai\azure-ai-projects\scripts\rle\release.py"),
)
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)
wheel = release.build_wheel(
    pathlib.Path(r"sdk\ai\azure-ai-projects"),
    "2.7.0b7.dev20261104+queuedv3.1",
    pathlib.Path(r"C:\temp\queued-v3-build"),
)
print(wheel)
'@ | python -
```

Two independent builds with this epoch produced the identical digest. The source
and exact installed wheel each passed 197 offline rollout tests. The repository
`python -m azpysdk.main verifywhl . --pypi` check also passed from the package
directory. These are local compatibility checks, not production qualification.
