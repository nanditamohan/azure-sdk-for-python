# RLE SDK beta releases

Build SDK wheels and publish the version registry to
[Azure/azure-sdk-for-python#49482](https://github.com/Azure/azure-sdk-for-python/pull/49482).
Prerequisites: Python with `build`, Git, committed SDK changes and public-branch
write access. A separate public checkout, `azure-sdk-for-python-rle-external`,
is created beside the source repository on first use; refresh it before later
releases. Use `--public-checkout <path>` only to override its location.

From the source repository root:

```powershell
$release = "sdk\ai\azure-ai-projects\scripts\rle\release.py"
python $release               # Patch (default): 2.7.1b1
python $release --bump minor  # Or minor: 2.8.0b1
python $release --bump major  # Or major: 3.0.0b1

# After testing the prepared wheel:
python $release --publish-only
```

Versioning bumps the highest core version in the SDK or registry, using a `b1`
suffix. Use `--publish` to prepare and publish in one step only when validated.
Authentication uses `GH_TOKEN`, `GITHUB_TOKEN`, or Git credentials; add
`--github-user <account>` when Git has multiple accounts.

Only wheels and the registry are published. Approved versions are preserved,
checksums verified, and the source SDK version left unchanged. Consumer setup
belongs in the [RLE samples documentation](https://github.com/sujit-kamireddy/rle-samples/blob/rle-public-sdk-wheels/README.md#rle-sdk-installation).
