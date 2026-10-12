# RLE SDK beta releases

Prepare wheels and the registry for
[Azure/azure-sdk-for-python#49488](https://github.com/Azure/azure-sdk-for-python/pull/49488).
Prerequisites: Python with `build`, Git, committed and tested SDK changes, and a
separate local checkout of `Azure/azure-sdk-for-python` on `rle-releases`.

From the source repository root, enter this scripts folder and provide that checkout:

```powershell
cd sdk\ai\azure-ai-projects\scripts\rle
python release.py --public-checkout C:\repos\azure-sdk-public
python release.py --public-checkout C:\repos\azure-sdk-public --bump minor
python release.py --public-checkout C:\repos\azure-sdk-public --bump major
```

The script checks that the public checkout uses the correct repository and branch,
is clean, and contains only allowed release changes. It refreshes the checkout
without merging or resetting your work and rechecks it before building. Diverged
branches fail. The script never switches branches, stages files, commits, or pushes.

Patch is the default. Versioning bumps the highest core version in the SDK or
registry with a `b1` suffix: `2.7.0b1` becomes `2.7.1b1`, `2.8.0b1`, or `3.0.0b1`.
The wheel and registry are left as unstaged changes in the provided checkout.
Artifacts live under `sdk/ai/azure-ai-projects/rle`, with registry channel `rle`.
New wheel URLs use versioned paths on the public release branch with SHA-256
validation. Approved versions are preserved and the source SDK version unchanged.

Review/test the prepared wheel, then stage, commit, and push both files manually.
Only your push updates the public release branch and registry; it does not update
`main`. Keep `rle-releases` available after the artifact PR is merged.
The script prints the prepared version, registry URL, and PR link.
Consumer setup belongs in the [RLE samples documentation](https://github.com/sujit-kamireddy/rle-samples/blob/main/README.md#rle-sdk-installation).
