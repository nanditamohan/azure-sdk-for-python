# RLE SDK beta releases

Build SDK wheels and publish the version registry to
[Azure/azure-sdk-for-python#49482](https://github.com/Azure/azure-sdk-for-python/pull/49482).
Prerequisites: Python with `build`, Git, committed SDK changes and public-branch
write access. Test SDK changes before releasing.

From the source repository root:

```powershell
$release = "sdk\ai\azure-ai-projects\scripts\rle\release.py"
python $release               # Patch (default): 2.7.1b1
python $release --bump minor  # Or minor: 2.8.0b1
python $release --bump major  # Or major: 3.0.0b1
```

Versioning bumps the highest core version in the SDK or registry, using a `b1`
suffix. Every invocation clones the public PR's release branch into a temporary folder,
builds the wheel, commits the wheel and registry, and pushes to update the PR.
The temporary folder is removed on success or failure; no separate checkout is needed.
On success, the script prints the published version, registry URL, and PR link.

Authentication uses `GH_TOKEN`, `GITHUB_TOKEN`, or Git credentials; add
`--github-user <account>` when Git has multiple accounts.

Only wheels and the registry are published. Approved versions are preserved,
checksums verified, and the source SDK version left unchanged. Consumer setup
belongs in the [RLE samples documentation](https://github.com/sujit-kamireddy/rle-samples/blob/rle-public-sdk-wheels/README.md#rle-sdk-installation).
