# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Opt-in durable Execute Rollout V3 transport.

The public AsyncRolloutClient owns this transport and its admission/deadline policy.
One driver serializes commands while a separate bounded receiver dispatches events.
"""

from __future__ import annotations

import asyncio  # pylint: disable=do-not-import-asyncio
import json
import logging
import re
import struct
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from azure.core.exceptions import AzureError
from azure.core.rest import HttpRequest
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.typing import Subprotocol

from ...operations._patch_rle_rollout import (
    DEFAULT_MAX_RESPONSE_BYTES,
    EXECUTE_ROLLOUT_API_VERSION,
    RLERolloutError,
    RLERolloutUnknownOutcomeError,
    build_rollout_websocket_url,
    deserialize_rollout_frame,
    serialize_rollout_frame,
)
from ...rle._rollout_models import RolloutRequest, RolloutResult

if TYPE_CHECKING:
    from ._rollout_client_async import AsyncRolloutClient

_LOGGER = logging.getLogger(__name__)
_WIRE_LOGGER = logging.Logger(__name__ + ".wire")
_WIRE_LOGGER.disabled = True
_MAX_RECONNECTS = 3
_PROTOCOL = "rle.execute-rollout.v3"
_RESULT_ROUTE = re.compile(
    r"/rle/v1\.0/subscriptions/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/resourceGroups/[A-Za-z0-9_.()-]+"
    r"/providers/Microsoft\.MachineLearningServices/workspaces/[A-Za-z0-9_-]+"
    r"/rl_environments/rollouts/(?P<rollout>[0-9a-f]{32})/result"
)
_SERVICE_CODES = frozenset(
    {
        "RolloutNotFound",
        "InvalidCursor",
        "QueuedDeliveryDisabled",
        "UnsupportedMessageType",
        "QueuedAcceptanceDisabled",
        "DuplicateAttachment",
        "TooManyAttachments",
        "RolloutIdAlreadyUsed",
        "InvalidRolloutRequest",
        "DeliveryUnavailable",
        "HistoryUnavailable",
        "DependencyUnavailable",
        "Forbidden",
        "Unauthorized",
        "queued_validation_failed",
        "queued_delegation_failed",
        "queued_execution_failed",
        "queued_budget_expired",
        "queued_attempt_timeout",
    }
)


def _error(code: str, rollout_id: Optional[str] = None) -> RLERolloutError:
    return RLERolloutError(
        None,
        code=code,
        rollout_id=rollout_id,
        message="Queued rollout delivery did not complete; execution was not replayed.",
    )


def _unique_object(pairs: Any) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field.")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number.")


def _object(data: bytes) -> dict[str, Any]:
    value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _command(kind: str, rollout_id: str, job_id: Optional[str], payload: Mapping[str, Any]) -> bytes:
    header = {"type": kind, "rollout_id": rollout_id}
    if job_id is not None:
        header["job_id"] = job_id
    encoded = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(encoded)) + encoded + json.dumps(payload, separators=(",", ":")).encode()


class _Pending:
    def __init__(
        self,
        request: bytes,
        deadline: float,
        limit: int,
        callback: Optional[Callable[[Mapping[str, Any]], None]],
    ) -> None:
        self.request = request
        self.deadline = deadline
        self.limit = min(limit, DEFAULT_MAX_RESPONSE_BYTES)
        self.callback = callback
        self.future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.submitted = False
        self.sequence: Optional[int] = None
        self.reconnects = 0


class _QueuedRolloutClient:
    def __init__(self, owner: AsyncRolloutClient) -> None:
        self._owner = owner
        if owner._job_id is not None and (
            not isinstance(owner._job_id, str) or re.fullmatch(r"ftjob-[0-9a-f]{24}", owner._job_id) is None
        ):
            raise ValueError("Queued job_id must have the canonical ftjob- format.")
        self._pending: dict[str, _Pending] = {}
        self._wake = asyncio.Event()
        self._driver: Optional[asyncio.Task] = None
        self._socket: Any = None
        self._expires = 0.0
        self._healthy = False

    async def execute(
        self,
        body: RolloutRequest,
        timeout: float,
        callback: Optional[Callable[[Mapping[str, Any]], None]],
        limit: int,
    ) -> RolloutResult:
        rollout_id = body.rollout_id
        if not isinstance(rollout_id, str) or re.fullmatch(r"[0-9a-f]{32}", rollout_id) is None:
            raise ValueError("Queued rollout_id must be a lowercase GUID without separators.")
        async with self._owner._lease(self._owner._admission, None):
            if rollout_id in self._pending:
                raise ValueError("rollout_id is already pending in this session")
            deadline = time.monotonic() + timeout
            # Only admitted calls retain serialized frames; reject invalid input before sending.
            frame = serialize_rollout_frame("execute", body)
            _object(frame[4 + struct.unpack(">I", frame[:4])[0] :])
            pending = _Pending(frame, deadline, limit, callback)
            self._pending[rollout_id] = pending
            if self._driver is None or self._driver.done():
                self._driver = asyncio.create_task(self._run())
                self._driver.add_done_callback(self._driver_done)
            self._wake.set()
            terminal_received = False
            try:
                terminal = await self._owner._wait_or_close(pending.future, deadline)
                terminal_received = True
                return await self._owner._wait_or_close(
                    asyncio.create_task(self._result(rollout_id, terminal, pending.limit)), deadline
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not terminal_received and pending.future.done() and not pending.future.cancelled():
                    terminal = pending.future.result()
                    if "result" in terminal:
                        return await self._result(rollout_id, terminal, pending.limit)
                    terminal_received = True
                if terminal_received:
                    raise _error("RolloutResultUnavailable", rollout_id) from None
                if pending.submitted:
                    raise RLERolloutUnknownOutcomeError(rollout_id=rollout_id) from None
                raise
            finally:
                self._pending.pop(rollout_id, None)
                if not pending.future.done():
                    pending.future.cancel()
                elif not pending.future.cancelled():
                    pending.future.exception()
                self._wake.set()

    def _driver_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Queued rollout receive/command driver failed.")
            self._fail("RolloutDeliveryFailed")

    def _fail(self, code: str) -> None:
        for rollout_id, pending in self._pending.items():
            if not pending.future.done():
                pending.future.set_exception(_error(code, rollout_id))

    async def _headers(self) -> tuple[dict[str, str], float]:
        config = self._owner._operations._config
        if config.credential is None or not config.credential_scopes:
            raise _error("RolloutCredentialMissing")
        try:
            user = await config.credential.get_token(*config.credential_scopes)
            finetune = await self._owner._get_finetune_token()
        except AzureError:
            raise _error("RolloutCredentialFailed") from None
        expiry = min(user.expires_on, finetune.expires_on) if finetune else user.expires_on
        tokens = (user.token, finetune.token) if finetune else (user.token,)
        if expiry - time.time() <= 60 or any(
            not token or any(ord(char) < 33 or ord(char) > 126 for char in token) for token in tokens
        ):
            raise _error("RolloutCredentialExpired")
        headers = {"Authorization": "Bearer " + user.token, "aml-user-token": user.token}
        if finetune:
            headers["x-ms-finetune-identity"] = finetune.token
        return headers, expiry

    async def _connect(self, deadline: float) -> None:
        from ._patch_rle_async import _connect_openenv_websocket

        headers, expiry = await self._headers()
        config = self._owner._operations._config
        self._socket = await _connect_openenv_websocket(
            build_rollout_websocket_url(
                config.endpoint,
                self._owner._name,
                self._owner._version,
                uuid.uuid4().hex,
                EXECUTE_ROLLOUT_API_VERSION,
            ),
            token=headers["aml-user-token"],
            additional_headers=headers,
            subprotocols=(Subprotocol(_PROTOCOL),),
            timeout=max(0.0, deadline - time.monotonic()),
            max_size=DEFAULT_MAX_RESPONSE_BYTES + 4100,
            close_timeout=5.0,
            logger=_WIRE_LOGGER,
            max_queue=16,
        )
        if self._socket.subprotocol != _PROTOCOL:
            raise _error("QueuedProtocolRequired")
        if expiry - time.time() <= 60:
            raise _error("RolloutCredentialExpired")
        self._expires = time.monotonic() + min(1740.0, expiry - time.time() - 60)

    async def _receive(self) -> None:
        while True:
            header, raw = deserialize_rollout_frame(await self._socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
            payload = _object(raw)
            self._dispatch(header, payload, len(raw))
            self._healthy = True

    def _dispatch(self, header: Mapping[str, Any], payload: dict[str, Any], size: int) -> None:
        rollout_id = header.get("rollout_id")
        kind = header.get("type")
        if not isinstance(rollout_id, str) or re.fullmatch(r"[0-9a-f]{32}", rollout_id) is None:
            raise ValueError("Invalid rollout correlation.")
        if kind not in ("accepted", "state", "progress", "completed", "failed", "error"):
            raise ValueError("Unknown queued frame type.")
        if payload.get("rollout_id", rollout_id) != rollout_id:
            raise ValueError("Conflicting rollout correlation.")
        pending = self._pending.get(rollout_id)
        if pending is None or pending.future.done():
            return  # A late frame after local detach is not a new execution.
        if size > pending.limit:
            pending.future.set_exception(_error("RolloutResponseTooLarge", rollout_id))
            return
        if kind in ("progress", "completed", "failed"):
            sequence = payload.get("sequence")
            attempt = payload.get("attempt_id")
            if "sequence" not in payload or "attempt_id" not in payload:
                raise ValueError("Missing durable event identity.")
            if sequence is None:
                if kind != "failed":
                    raise ValueError("Missing durable sequence.")
            elif (
                type(sequence) is not int
                or not 0 <= sequence <= 2**63 - 1
                or (
                    pending.sequence is not None
                    and (sequence < pending.sequence or (kind == "progress" and sequence == pending.sequence))
                )
            ):
                raise ValueError("Invalid durable sequence.")
            if attempt is not None and (not isinstance(attempt, str) or not attempt or len(attempt) > 128):
                raise ValueError("Invalid durable attempt.")
            if kind in ("progress", "completed") and attempt is None:
                raise ValueError("Missing durable attempt.")
            pending.sequence = sequence if sequence is not None else pending.sequence
        if kind == "accepted":
            if payload.get("phase") != "queued" or payload.get("attempt_id") is not None:
                raise ValueError("Invalid acceptance.")
        elif kind == "state":
            if payload.get("phase") not in ("queued", "running", "finalizing", "completed", "failed"):
                raise ValueError("Invalid queued state.")
            # latest_sequence and echoed last_sequence are NOT received event cursors.
        elif kind == "progress":
            if not isinstance(payload.get("progress"), dict):
                raise ValueError("Invalid durable progress.")
            if pending.callback is not None:
                try:
                    pending.callback(
                        {
                            **payload["progress"],
                            "rollout_id": rollout_id,
                            "sequence": pending.sequence,
                            "attempt_id": payload["attempt_id"],
                        }
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    pending.future.set_exception(_error("RolloutProgressCallbackFailed", rollout_id))
        elif kind == "completed":
            pending.future.set_result(payload)
        elif kind in ("failed", "error"):
            code = payload.get("code")
            if (
                not isinstance(code, str)
                or not isinstance(payload.get("message"), str)
                or not isinstance(payload.get("retryable"), bool)
                or (kind == "failed" and payload["retryable"])
            ):
                raise ValueError("Invalid queued error.")
            pending.future.set_exception(_error(code if code in _SERVICE_CODES else "QueuedRolloutFailed", rollout_id))

    async def _drop(self, receive: Optional[asyncio.Task]) -> None:
        if receive is not None:
            receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)
        if self._socket is not None:
            socket, self._socket = self._socket, None
            try:
                await asyncio.wait_for(socket.close(), timeout=5)
            except (OSError, TimeoutError, WebSocketException):
                _LOGGER.warning("Queued rollout socket close failed.")
                transport = getattr(socket, "transport", None)
                if transport is not None:
                    transport.abort()

    async def _send(self, message: bytes, deadline: float) -> None:
        await asyncio.wait_for(self._socket.send(message), timeout=min(30, max(0, deadline - time.monotonic())))

    async def _run(self) -> None:
        receive: Optional[asyncio.Task] = None
        attached: set[str] = set()
        failures = 0
        try:
            while not self._owner._closed:
                self._wake.clear()
                active = {key: value for key, value in self._pending.items() if not value.future.done()}
                if not active and self._socket is None:
                    failures = 0
                    await self._wake.wait()
                    continue
                try:
                    if self._socket is None:
                        self._healthy = False
                        deadline = min(value.deadline for value in active.values())
                        await asyncio.wait_for(
                            self._connect(deadline), timeout=min(30, max(0, deadline - time.monotonic()))
                        )
                        attached.clear()
                    if receive is None:
                        receive = asyncio.create_task(self._receive())
                    for rollout_id in tuple(attached - active.keys()):
                        await self._send(_command("detach", rollout_id, self._owner._job_id, {}), time.monotonic() + 5)
                        attached.remove(rollout_id)
                    for rollout_id, pending in active.items():
                        if receive.done():
                            receive.result()
                        if rollout_id in attached or pending.future.done():
                            continue
                        if time.monotonic() >= self._expires:
                            await self._drop(receive)
                            receive = None
                            break
                        if pending.submitted:
                            pending.reconnects += 1
                            if pending.reconnects > _MAX_RECONNECTS:
                                pending.future.set_exception(RLERolloutUnknownOutcomeError(rollout_id=rollout_id))
                                continue
                            frame = _command(
                                "attach", rollout_id, self._owner._job_id, {"last_sequence": pending.sequence}
                            )
                        else:
                            frame = pending.request
                            # An interrupted send may already have reached durable admission.
                            pending.submitted = True
                        await self._send(frame, pending.deadline)
                        attached.add(rollout_id)
                    if receive is None:
                        continue
                    wake = asyncio.create_task(self._wake.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (receive, wake),
                            timeout=max(0, self._expires - time.monotonic()),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        wake.cancel()
                        await asyncio.gather(wake, return_exceptions=True)
                    if receive in done:
                        receive.result()
                    elif not done:
                        await self._drop(receive)
                        receive = None
                except (ConnectionClosed, OSError, EOFError, TimeoutError, WebSocketException) as exc:
                    await self._drop(receive)
                    receive = None
                    failures = 1 if self._healthy else failures + 1
                    status = exc.response.status_code if isinstance(exc, InvalidStatus) else None
                    closes = (exc.rcvd, exc.sent) if isinstance(exc, ConnectionClosed) else ()
                    if any(close is not None and close.code == 1009 for close in closes):
                        self._fail("RolloutResponseTooLarge")
                    elif any(close is not None and close.code in (1002, 1003) for close in closes):
                        self._fail("InvalidRolloutFrame")
                    elif (
                        status is not None and status not in (408, 429, 500, 502, 503, 504)
                    ) or failures > _MAX_RECONNECTS:
                        for rollout_id, pending in self._pending.items():
                            if not pending.future.done():
                                pending.future.set_exception(
                                    RLERolloutUnknownOutcomeError(rollout_id=rollout_id)
                                    if pending.submitted
                                    else _error("RolloutConnectionFailed", rollout_id)
                                )
                    else:
                        _LOGGER.warning("Queued rollout connection lost; bounded attachment recovery.")
                        await asyncio.sleep(min(float(failures), 3))
                except RLERolloutError as exc:
                    self._fail(exc.code or "RolloutDeliveryFailed")
                    await self._drop(receive)
                    receive = None
                except (ValueError, UnicodeError, RecursionError):
                    self._fail("InvalidRolloutFrame")
                    await self._drop(receive)
                    receive = None
        finally:
            await self._drop(receive)

    async def _result(self, rollout_id: str, terminal: dict[str, Any], limit: int) -> RolloutResult:
        if "result" in terminal and "result_url" not in terminal:
            result = terminal["result"]
        elif "result_url" in terminal and "result" not in terminal:
            route = terminal["result_url"]
            match = _RESULT_ROUTE.fullmatch(route) if isinstance(route, str) else None
            if match is None or match["rollout"] != rollout_id or any(part in (".", "..") for part in route.split("/")):
                raise _error("InvalidRolloutResultReference", rollout_id)
            result = await self._fetch_result(rollout_id, limit)
        else:
            result = None
        if not isinstance(result, dict) or result.get("rollout_id") != rollout_id:
            raise _error("InvalidRolloutResult", rollout_id)
        return RolloutResult(result)

    async def _fetch_result(self, rollout_id: str, limit: int) -> dict[str, Any]:
        client = self._owner._operations._pipeline_client
        config = self._owner._operations._config
        headers, _ = await self._headers()
        headers["Accept"] = "application/json"
        request = HttpRequest(
            "GET",
            f"/rl_environments/rollouts/{rollout_id}/result",
            params={"api-version": "2025-05-01"},
            headers=headers,
        )
        request.url = client.format_url(request.url, endpoint=config.endpoint)
        try:
            pipeline_response = await client._pipeline.run(request, stream=True, retry_total=0, permit_redirects=False)
            response = pipeline_response.http_response
            try:
                if response.status_code != 200:
                    raise _error("RolloutResultUnavailable", rollout_id)
                data = bytearray()
                async for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise _error("RolloutResultTooLarge", rollout_id)
                    data.extend(chunk)
                return _object(bytes(data))
            finally:
                await response.close()
        except RLERolloutError:
            raise
        except (AzureError, ValueError, UnicodeError, RecursionError):
            raise _error("RolloutResultUnavailable", rollout_id) from None

    async def close(self) -> None:
        for rollout_id, pending in self._pending.items():
            if not pending.future.done():
                pending.future.set_exception(
                    RLERolloutUnknownOutcomeError(rollout_id=rollout_id)
                    if pending.submitted
                    else _error("RolloutSessionClosed", rollout_id)
                )
        if self._driver is not None:
            self._driver.cancel()
            await asyncio.gather(self._driver, return_exceptions=True)
