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
