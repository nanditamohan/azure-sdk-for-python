# RLE SDK beta releases

Prepare wheels and the registry for
[Azure/azure-sdk-for-python#49482](https://github.com/Azure/azure-sdk-for-python/pull/49482).
Prerequisites: Python with `build`, Git, committed and tested SDK changes, and a
separate local checkout of `Azure/azure-sdk-for-python` on `rle-external-releases`.

From the source repository root, enter this scripts folder and provide that checkout:

```powershell
cd sdk\ai\azure-ai-projects\scripts\rle
python release.py --public-checkout C:\repos\azure-sdk-public
python release.py --public-checkout C:\repos\azure-sdk-public --bump minor
python release.py --public-checkout C:\repos\azure-sdk-public --bump major
```

The script verifies the repository, branch, and clean state, then pulls with
`--ff-only` and rechecks the checkout before building. Diverged branches fail;
the script never switches branches, resets your work, stages files, commits, or pushes.

Patch is the default. Versioning bumps the highest core version in the SDK or
registry with a `b1` suffix: `2.7.0b1` becomes `2.7.1b1`, `2.8.0b1`, or `3.0.0b1`.
The wheel and registry are left as unstaged changes in the provided checkout.
New wheel URLs use versioned paths on the public release branch with SHA-256
validation. Approved versions are preserved and the source SDK version unchanged.

Review/test the prepared wheel, then stage, commit, and push both files manually.
Only your push updates the public registry and PR; it does not update `main`.
The script prints the prepared version, registry URL, and PR link.
Consumer setup belongs in the [RLE samples documentation](https://github.com/sujit-kamireddy/rle-samples/blob/rle-public-sdk-wheels/README.md#rle-sdk-installation).
