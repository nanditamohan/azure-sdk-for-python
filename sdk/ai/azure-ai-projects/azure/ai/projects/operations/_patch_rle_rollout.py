# pylint: disable=line-too-long,useless-suppression
# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Shared request/response plumbing for the RLE Execute Rollout operation.

The sync and async operation groups differ only in how they await the pipeline and the
credential, so everything else -- URL construction, body shaping, error surfacing -- lives here
and is used by both. Keeping it in one place is what stops the two surfaces from drifting into
sending subtly different requests.
"""

from __future__ import annotations

import json
import math
import struct
import uuid
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from azure.core.exceptions import HttpResponseError
from azure.core.rest import HttpRequest

from ..rle._rollout_models import RolloutPolicy, RolloutRequest, RolloutResult, SamplingOptions

# The Foundry data plane gates ``:executeRollout`` on this preview api-version. It is
# deliberately not ``config.api_version`` (``v1``), which the instance-level RLE routes use:
# the two are separate contracts on the same endpoint, and pinning the rollout route to the
# version it was shipped under keeps a client-wide api_version override from silently moving it.
EXECUTE_ROLLOUT_API_VERSION = "2025-11-15-preview"
EXECUTE_ROLLOUT_SUBPROTOCOLS = ("rle.execute-rollout.v2", "rle.execute-rollout.v1")
DEFAULT_ROLLOUT_TIMEOUT_S = 1860.0
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# The classic AzureML forwarded-user-token header. RLE requires it in addition to
# ``Authorization`` and forwards it unchanged to Capture Proxy, which presents it to Loom as a
# raw bearer when opening the rollout's sampling session. Both consumers require the
# ``https://ai.azure.com`` audience, which is the client's own credential scope, so the SDK
# acquires it rather than making the caller plumb a second token.
FORWARDED_TOKEN_HEADER = "aml-user-token"

_ROLLOUT_PATH_TEMPLATE = "/rl_environments/{name}/versions/{version}:executeRollout"


class RLERolloutError(HttpResponseError):
    """An Execute Rollout call that the service refused or could not complete.

    RLE answers failures with a flat ``{"code", "message"}`` body rather than the nested
    ``error`` envelope azure-core expects, so the code and message are lifted onto the exception
    here instead of being left buried in an unparsed response.

    :ivar code: Stable machine-readable failure code, such as ``RolloutDependencyFailed``.
    :vartype code: str or None
    :ivar rollout_id: The rollout this failure belongs to, echoed for log correlation.
    :vartype rollout_id: str or None
    """

    def __init__(
        self,
        response: Any,
        *,
        code: Optional[str] = None,
        message: Optional[str] = None,
        rollout_id: Optional[str] = None,
    ) -> None:
        self.code = code
        self.rollout_id = rollout_id
        detail = message or "the RLE service did not complete the rollout"
        prefix = f"{code}: " if code else ""
        suffix = f" (rollout_id={rollout_id})" if rollout_id else ""
        composed = f"{prefix}{detail}{suffix}"
        super().__init__(message=composed, response=response)
        # HttpResponseError replaces the supplied message whenever it manages to parse the body
        # as OData, which RLE's flat {code, message} does satisfy. That would drop the rollout id
        # -- the one value that leads to the logged cause -- so the composed message is restored
        # here. ``args`` carries it too, because Exception.__str__ reads args rather than
        # ``message``. The parsed ``error`` is left intact for callers that read it.
        self.message = composed
        self.args = (composed,)


class RLERolloutUnknownOutcomeError(RLERolloutError):
    """Submitted work may still be executing; the SDK never replays it.

    ``rollout_id`` is the original identifier for operator correlation. Closing or
    cancelling local work does not cancel execution on the service.
    """

    def __init__(
        self,
        *,
        rollout_id: str,
        message: str = "The submitted rollout outcome could not be determined.",
        rollout_status: Optional[str] = None,
    ) -> None:
        self.rollout_status = rollout_status
        super().__init__(None, code="AmbiguousRolloutOutcome", message=message, rollout_id=rollout_id)


class _RolloutConnectionLost(Exception):
    """The connection cannot account for pending execution."""


def build_rollout_request(
    *,
    environment_name: str,
    environment_version: str,
    body: RolloutRequest,
    forwarded_token: str,
    api_version: str = EXECUTE_ROLLOUT_API_VERSION,
) -> HttpRequest:
    """Shape one ``:executeRollout`` request, relative to the client's project endpoint.

    :keyword environment_name: Published environment name.
    :paramtype environment_name: str
    :keyword environment_version: Exact version to run. Never resolved to "latest": a rollout is
     pinned so the trajectory it returns names the code that produced it.
    :paramtype environment_version: str
    :keyword body: The rollout request.
    :paramtype body: ~azure.ai.projects.models.RolloutRequest
    :keyword forwarded_token: Bearer token forwarded to Capture Proxy for Loom sampling.
    :paramtype forwarded_token: str
    :keyword api_version: Data-plane api-version for this route.
    :paramtype api_version: str
    :return: The prepared request.
    :rtype: ~azure.core.rest.HttpRequest
    """
    url = _ROLLOUT_PATH_TEMPLATE.format(
        name=quote(environment_name, safe=""),
        version=quote(environment_version, safe=""),
    )
    return HttpRequest(
        method="POST",
        url=url,
        params={"api-version": api_version},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            FORWARDED_TOKEN_HEADER: forwarded_token,
        },
        content=json.dumps(body.as_dict()),
    )


def build_rollout_body(
    *,
    task: Any,
    policy: RolloutPolicy,
    sampling: Optional[SamplingOptions] = None,
    rollout_id: Optional[str] = None,
    job_id: Optional[str] = None,
    harness: Optional[Mapping[str, Any]] = None,
) -> RolloutRequest:
    """Assemble a rollout request, generating an id when the caller did not supply one.

    The generated id is a bare-hex UUID, the form RLE reserves per project. A caller that wants
    to correlate a rollout with its own records should pass ``rollout_id`` instead, since the
    service returns whatever it was given unchanged.

    :keyword task: Opaque task record forwarded to the environment's reset.
    :paramtype task: any
    :keyword policy: Where this rollout's weights come from.
    :paramtype policy: ~azure.ai.projects.models.RolloutPolicy
    :keyword sampling: How completions are sampled and rendered.
    :paramtype sampling: ~azure.ai.projects.models.SamplingOptions or None
    :keyword rollout_id: Caller-supplied identifier. Generated when omitted.
    :paramtype rollout_id: str or None
    :keyword job_id: Optional job identifier attached to the request.
    :paramtype job_id: str or None
    :keyword harness: Optional per-request harness override.
    :paramtype harness: mapping[str, any] or None
    :return: The assembled request.
    :rtype: ~azure.ai.projects.models.RolloutRequest
    """
    if task is None:
        raise ValueError("task is required: it is the record the environment resets on")
    if policy is None:
        raise ValueError("policy is required: a rollout must name where its weights come from")
    return RolloutRequest(
        rollout_id=rollout_id or uuid.uuid4().hex,
        task=task,
        policy=policy,
        sampling=sampling,
        **({"job_id": job_id} if job_id is not None else {}),
        **({"harness": dict(harness)} if harness is not None else {}),
    )


def validate_rollout_session(environment_name: str, environment_version: str, limit: int) -> None:
    if not environment_name or not environment_version:
        raise ValueError("environment_name and environment_version are required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
        raise ValueError("max_concurrent_rollouts must be an integer between 1 and 64")


def validate_rollout_options(timeout: float, max_response_bytes: int) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be a positive integer")


def build_rollout_websocket_url(
    endpoint: str, environment_name: str, environment_version: str, connection_id: str, api_version: str
) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() != "https":
        raise ValueError("project endpoint must use https for authenticated WebSockets")
    path = (
        f"/rl_environments/{quote(environment_name, safe='')}/versions/{quote(environment_version, safe='')}"
        f"/instance_groups/_execute_rollout/instances/{quote(connection_id, safe='')}/openenv/ws"
    )
    query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() != "api-version"
    ]
    query.append(("api-version", api_version))
    return urlunsplit(("wss", parsed.netloc, parsed.path.rstrip("/") + path, urlencode(query), ""))


def serialize_rollout_frame(message_type: str, body: RolloutRequest) -> bytes:
    header = {"type": message_type, "rollout_id": body.rollout_id}
    if body.job_id:
        header["job_id"] = body.job_id
    encoded = json.dumps(header, separators=(",", ":")).encode()
    payload = json.dumps(body.as_dict(), separators=(",", ":")).encode()
    if len(encoded) > 4096 or 4 + len(encoded) + len(payload) > 4 * 1024 * 1024:
        raise ValueError("Execute Rollout request exceeded the 4 KiB header or 4 MiB total message limit")
    return struct.pack(">I", len(encoded)) + encoded + payload


def _unique_header(pairs: Any) -> dict[str, Any]:
    header: dict[str, Any] = {}
    for key, value in pairs:
        if key in header:
            raise ValueError("Execute Rollout returned duplicate frame header keys")
        header[key] = value
    return header


def deserialize_rollout_frame(message: Any, max_response_bytes: int) -> "tuple[Mapping[str, Any], bytes]":
    if not isinstance(message, bytes) or len(message) < 4:
        raise ValueError("Execute Rollout returned an invalid binary frame")
    size = struct.unpack(">I", message[:4])[0]
    if not 0 < size <= 4096 or len(message) < 4 + size:
        raise ValueError("Execute Rollout returned an invalid frame header")
    header = json.loads(message[4 : 4 + size], object_pairs_hook=_unique_header)
    if not isinstance(header, Mapping) or not isinstance(header.get("type"), str):
        raise ValueError("Execute Rollout returned an invalid frame header")
    if header["type"] != "server-draining" and not isinstance(header.get("rollout_id"), str):
        raise ValueError("Execute Rollout returned a frame without a rollout identifier")
    payload = message[4 + size :]
    if len(payload) > min(max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES):
        raise ValueError("Execute Rollout response exceeded max_response_bytes or the 8 MiB protocol limit")
    return header, payload


def deserialize_rollout_payload(payload: bytes, rollout_id: str) -> RolloutResult:
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise ValueError("Execute Rollout returned a non-object result")
    result = RolloutResult(value)
    if result.rollout_id != rollout_id:
        raise ValueError("Execute Rollout returned a result for a different rollout")
    return result


def rollout_progress(payload: bytes, rollout_id: str) -> Mapping[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise ValueError("Execute Rollout returned non-object progress")
    return {**value, "rollout_id": rollout_id}


def rollout_error_from_payload(payload: bytes, rollout_id: str) -> RLERolloutError:
    value = json.loads(payload)
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("code"), str)
        or not isinstance(value.get("message"), str)
    ):
        raise ValueError("Execute Rollout returned an invalid error")
    return RLERolloutError(None, code=value.get("code"), message=value.get("message"), rollout_id=rollout_id)


def deserialize_rollout_response(response: Any, rollout_id: str) -> RolloutResult:
    """Turn a completed pipeline response into a result, or raise with the service's own code.

    :param response: The HTTP response.
    :type response: any
    :param rollout_id: The requested rollout id, used to correlate a failure.
    :type rollout_id: str
    :return: The rollout result.
    :rtype: ~azure.ai.projects.models.RolloutResult
    :raises ~azure.ai.projects.operations.RLERolloutError: If the service did not return 200.
    """
    if response.status_code != 200:
        code, message = _read_error(response)
        raise RLERolloutError(response, code=code, message=message, rollout_id=rollout_id)
    return RolloutResult(response.json())


def _read_error(response: Any) -> "tuple[Optional[str], Optional[str]]":
    """Extract RLE's flat ``{code, message}``, tolerating anything else.

    A failure to parse an error must never replace the error: if the body is not the shape we
    expect -- a gateway's HTML, a truncated response -- the status code still reaches the caller
    through the raised exception, which is more than a parse error would leave them with.
    """
    try:
        body = response.json()
    except Exception:  # pylint: disable=broad-except
        return None, None
    if not isinstance(body, Mapping):
        return None, None
    code = body.get("code")
    message = body.get("message")
    # Also accept the nested envelope, in case a gateway ahead of RLE answers instead of RLE.
    error = body.get("error")
    if isinstance(error, Mapping):
        code = code or error.get("code")
        message = message or error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )
