# Queued V3 development artifact: opaque managed job IDs

Version `2.7.0b7.dev20261104+queuedv3.3` replaces an incorrect fixed
24-hex job-ID restriction with the service's bounded opaque ID contract.
Optional job IDs have a case-sensitive `ftjob-` prefix, only ASCII letters,
digits, `.`, `_`, `~`, and `-`, and at most 991 characters total. Managed
32-hex, legacy 24-hex and safe opaque suffixes are accepted unchanged.
Nothing is trimmed, truncated, decoded, normalized or synthesized.

The original job ID is retained in execute header/body and attach/detach
commands. Rollout IDs still require lowercase GUIDs without separators.
V3 negotiation, credential handling, bounds, deadlines, received cursors
and no-execute-replay semantics are unchanged, including the `.2` expiry fix.

SHA-256: `ee4630608daeda2a539e3a2f98f2da1dc90e89a577c61dc23edaff4ac6afbf87`.
Size: 601096 bytes. See [provenance](provenance.json) for source/build identity.
This is a new development-only artifact, not an official release or PyPI
publication. Only this latest development wheel is retained in the source PR;
previously published immutable artifact URLs and registry/latest remain unchanged.

```python
async with project_client.rle.get_rollout_client(
    environment_name,
    environment_version,
    job_id=training_job_id,
    protocol_version="v3",
    finetune_credential=finetune_credential,
) as session:
    result = await session.execute_rollout(task=task, policy=policy)
```

Omitting `protocol_version` preserves legacy defaults. Explicit V3 never falls
back. Pin the verified immutable public artifact URL with its SHA-256:

```text
https://raw.githubusercontent.com/swatisinghvi99/azure-sdk-for-python/9d53899d688ac00cf239d5c25d01c37dd44f6116/sdk/ai/azure-ai-projects/rle/wheels/2.7.0b7.dev20261104+queuedv3.3/azure_ai_projects-2.7.0b7.dev20261104+queuedv3.3-py3-none-any.whl#sha256=ee4630608daeda2a539e3a2f98f2da1dc90e89a577c61dc23edaff4ac6afbf87
```

This artifact is published on the authorized public fork only; the earlier
upstream distribution PR was closed unmerged. A copy in this private repository
still requires authorized authenticated staging.

The source and exact installed wheel each passed 226 offline rollout tests.
Public-factory real-WebSocket tests cover managed 32-hex, legacy 24-hex, safe
opaque IDs, 991-character boundaries and optional IDs through execute, cursor-0
attachment recovery and detach. Twenty-one unsafe/type/overlength cases fail
before credential lookup or connection. Two independent builds matched;
metadata/RECORD hashes and all 62 source module comparisons passed, as did
targeted Python 3.10 transport type and 120-column Black checks.

This fixes a local pre-handshake validation failure; offline tests do not prove
deployed service acceptance or completed training. No cloud retry, deployment,
image publication, permissions change or merge is performed. Consumers must
rebuild/select an image containing the new pin. Full release gates were not run.
