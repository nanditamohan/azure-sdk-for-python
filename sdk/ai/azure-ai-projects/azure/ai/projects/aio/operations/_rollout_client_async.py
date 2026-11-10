# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Execute Rollout client on the existing OpenEnv WebSocket transport."""

from __future__ import annotations

import asyncio  # pylint: disable=do-not-import-asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional
from urllib.parse import quote

from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.rest import HttpRequest
from websockets.exceptions import ConnectionClosed
from websockets.typing import Subprotocol

from ...operations._patch_rle_rollout import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_ROLLOUT_TIMEOUT_S,
    EXECUTE_ROLLOUT_API_VERSION,
    EXECUTE_ROLLOUT_SUBPROTOCOLS,
    RLERolloutError,
    RLERolloutUnknownOutcomeError,
    _RolloutConnectionLost,
    build_rollout_body,
    build_rollout_websocket_url,
    deserialize_rollout_frame,
    deserialize_rollout_payload,
    rollout_error_from_payload,
    rollout_progress,
    serialize_rollout_frame,
    validate_rollout_options,
    validate_rollout_session,
)
from ...rle._rollout_models import HarnessConfiguration, RolloutPolicy, RolloutResult, SamplingOptions

_LOGGER = logging.getLogger(__name__)
_MAX_CONCURRENT_ROLLOUTS = 32
_RECOVERY_API_VERSION = "2025-05-01"
_RECOVERY_POLL_INTERVAL = 30
_RECOVERY_RETRY_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})

if TYPE_CHECKING:
    from ._queued_rollout_client_async import _QueuedRolloutClient


class _Pending:
    def __init__(self, callback: Any, limit: int, deadline: float) -> None:
        self.future = asyncio.get_running_loop().create_future()
        self.callback = callback
        self.limit = limit
        self.terminal = False
        self.deadline = deadline
        self.lookup: Optional[asyncio.Task] = None


class _Connection:
    def __init__(self, socket: Any, expires: float) -> None:
        self.socket = socket
        self.expires = expires
        self.pending: dict[str, _Pending] = {}
        self.receiver: Optional[asyncio.Task] = None
        self.draining = False
        self.replaceable = asyncio.Event()
        self.replaceable.set()
        self.closing: Optional[asyncio.Task] = None


class AsyncRolloutClient:
    """An event-loop-local shared session with SDK admission and deployment handoff.

    Sessions default to V4 durable queued delivery and original-ID attachment recovery.
    V4 always downloads the full result over HTTP after a completion notification.
    Select ``protocol_version="v3"`` for V3, or explicitly pass ``None`` for legacy
    v2/v1 negotiation and the drain/HTTP recovery described below.

    The timeout starts after concurrency admission. By default, renewal waits for pending
    calls to settle before replacing the socket. Server-managed token renewal
    disables age/token-based replacement. Unknown submitted outcomes affect only
    their awaiting calls, not new admission or server execution. Draining sockets retain existing
    results while replacements accept new work. Only explicit pre-execution
    ServerDraining rejections can be resubmitted. A disconnected socket's pending
    rollouts are recovered through HTTP lookup without replaying execution.
    """

    def __init__(
        self,
        operations: Any,
        environment_name: str,
        environment_version: str,
        job_id: Optional[str] = None,
        *,
        server_managed_token_renewal: bool = False,
        finetune_credential: Optional[AsyncTokenCredential] = None,
        protocol_version: Optional[str] = "v4",
    ) -> None:
        validate_rollout_session(environment_name, environment_version)
        if not isinstance(server_managed_token_renewal, bool):
            raise ValueError("server_managed_token_renewal must be a bool")
        if protocol_version not in (None, "v3", "v4"):
            raise ValueError('protocol_version must be None (legacy), "v3", or "v4"')
        if finetune_credential is not None and server_managed_token_renewal:
            raise ValueError("Separate fine-tuning credentials require client-managed token renewal.")
        if protocol_version in ("v3", "v4") and server_managed_token_renewal:
            raise ValueError("Queued V3/V4 requires client-managed token renewal.")
        self._operations = operations
        self._finetune_credential = finetune_credential
        self._server_managed_token_renewal = server_managed_token_renewal
        self._name = environment_name
        self._version = environment_version
        self._job_id = job_id
        self._default_max_response_bytes = 32 * 1024 * 1024 if protocol_version == "v4" else DEFAULT_MAX_RESPONSE_BYTES
        self._admission = asyncio.Semaphore(_MAX_CONCURRENT_ROLLOUTS)
        self._send_lock = asyncio.Lock()
        self._active: Optional[_Connection] = None
        self._connections: set[_Connection] = set()
        self._pending_ids: set[str] = set()
        self._recoveries: dict[str, _Pending] = {}
        self._recovery_worker: Optional[asyncio.Task] = None
        self._closed_event = asyncio.Event()
        self._admission_stopped = asyncio.Event()
        self._queued: Optional[_QueuedRolloutClient] = None
        if protocol_version in ("v3", "v4"):
            from ._queued_rollout_client_async import _QueuedRolloutClient

            self._queued = _QueuedRolloutClient(self, protocol_version)

    @property
    def _closed(self) -> bool:
        return self._closed_event.is_set()

    @property
    def _uncertain(self) -> bool:
        return self._admission_stopped.is_set() and not self._closed

    async def __aenter__(self) -> "AsyncRolloutClient":
        self._check()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def _check(self) -> None:
        if self._closed:
            raise RLERolloutError(None, code="RolloutSessionClosed", message="The rollout session is closed.")
        if self._uncertain:
            raise RLERolloutError(
                None,
                code="RolloutSessionUnaccounted",
                message="An earlier outcome is unaccounted; this session cannot admit more work.",
            )

    def _stop_admission(self) -> None:
        self._admission_stopped.set()

    async def _wait_or_close(
        self, operation: asyncio.Future, deadline: Optional[float], *, admission: bool = False
    ) -> Any:
        signal = self._admission_stopped if admission else self._closed_event
        stopped = asyncio.create_task(signal.wait())
        try:
            done, _ = await asyncio.wait(
                (operation, stopped),
                timeout=None if deadline is None else max(0.0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return operation.result()
            if signal.is_set():
                self._check()
            raise asyncio.TimeoutError()
        finally:
            stopped.cancel()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, stopped, return_exceptions=True)

    @asynccontextmanager
    async def _lease(self, resource: Any, deadline: Optional[float]) -> Any:
        acquired = asyncio.create_task(resource.acquire())
        try:
            await self._wait_or_close(acquired, deadline, admission=True)
            self._check()
            yield
        finally:
            if acquired.done() and not acquired.cancelled() and acquired.exception() is None and acquired.result():
                resource.release()

    async def _close_connection(self, connection: _Connection) -> None:
        try:
            if connection.receiver is not None:
                connection.receiver.cancel()
                await asyncio.gather(connection.receiver, return_exceptions=True)
        finally:
            try:
                await asyncio.wait_for(connection.socket.close(), timeout=5.0)
            except BaseException:
                transport = getattr(connection.socket, "transport", None)
                if transport is not None:
                    transport.abort()
                raise
            finally:
                self._connections.discard(connection)
                if self._active is connection:
                    self._active = None

    def _retire_connection(self, connection: _Connection) -> asyncio.Task:
        if connection.closing is None:
            connection.closing = asyncio.create_task(self._close_connection(connection))
            connection.closing.add_done_callback(self._report_close_failure)
        return connection.closing

    @staticmethod
    def _report_close_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("Failed to close a retired Execute Rollout WebSocket.", exc_info=task.exception())

    @staticmethod
    def _mark_draining(connection: _Connection) -> None:
        connection.draining = True
        connection.replaceable.set()

    async def _connect(self, deadline: float) -> _Connection:
        from ._patch_rle_async import _connect_openenv_websocket

        self._check()
        if self._active and not self._active.draining and time.monotonic() < self._active.expires:
            return self._active
        if self._active:
            previous = self._active
            await self._wait_or_close(asyncio.create_task(previous.replaceable.wait()), deadline, admission=True)
            self._check()
            if not previous.draining or not previous.pending:
                await self._retire_connection(previous)
            if self._active is previous:
                self._active = None
        self._check()
        config = self._operations._config  # pylint: disable=protected-access
        credential = getattr(config, "credential", None)
        scopes = getattr(config, "credential_scopes", None)
        if credential is None or not scopes:
            raise RLERolloutError(
                None, code="RolloutCredentialMissing", message="A credential and scopes are required."
            )
        token = await credential.get_token(*scopes)
        if not token.token or token.expires_on - time.time() <= 60:
            raise RLERolloutError(
                None,
                code="RolloutCredentialExpired",
                message="The credential must report a valid token with more than 60 seconds remaining.",
            )
        finetune_token = await self._get_finetune_token()
        token_expiry = min(token.expires_on, finetune_token.expires_on) if finetune_token else token.expires_on
        headers = {"aml-user-token": token.token}
        if finetune_token:
            headers["x-ms-finetune-identity"] = finetune_token.token
        socket = await _connect_openenv_websocket(
            build_rollout_websocket_url(
                config.endpoint, self._name, self._version, uuid.uuid4().hex, EXECUTE_ROLLOUT_API_VERSION
            ),
            token=token.token,
            additional_headers=headers,
            subprotocols=tuple(Subprotocol(value) for value in EXECUTE_ROLLOUT_SUBPROTOCOLS),
            timeout=max(0.0, deadline - time.monotonic()),
            max_size=DEFAULT_MAX_RESPONSE_BYTES + 4100,
            close_timeout=5.0,
        )
        expires = (
            float("inf")
            if self._server_managed_token_renewal
            else time.monotonic() + min(1740.0, token_expiry - time.time() - 60.0)
        )
        connection = _Connection(socket, expires)
        if (
            self._closed or self._uncertain
            or time.monotonic() >= connection.expires
            or token_expiry - time.time() <= 60
        ):
            await self._close_connection(connection)
            self._check()
            raise RLERolloutError(None, code="RolloutCredentialExpired", message="Token expired during the handshake.")
        self._active = connection
        self._connections.add(connection)
        connection.receiver = asyncio.create_task(self._receive(connection))
        return connection

    async def _get_finetune_token(self) -> Optional[AccessToken]:
        if self._finetune_credential is None:
            return None
        scopes = self._operations._config.credential_scopes  # pylint: disable=protected-access
        token = await self._finetune_credential.get_token(*scopes)
        if not token.token or token.expires_on - time.time() <= 60:
            raise RLERolloutError(
                None, code="RolloutFineTuneCredentialExpired",
                message="The fine-tuning credential must have more than 60 seconds remaining.",
            )
        return token

    async def _receive(self, connection: _Connection) -> None:
        try:
            while not self._closed:
                header, payload = deserialize_rollout_frame(await connection.socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
                if header["type"] == "server-draining":
                    self._mark_draining(connection)
                    if not connection.pending:
                        self._retire_connection(connection)
                        return
                    continue
                rollout_id = header["rollout_id"]
                pending = connection.pending.get(rollout_id)
                if pending is None or pending.future.done():
                    continue
                try:
                    if len(payload) > pending.limit:
                        raise ValueError("Execute Rollout response exceeded max_response_bytes")
                    if header["type"] == "progress":
                        progress = rollout_progress(payload, rollout_id)
                        if pending.callback is not None:
                            pending.callback(progress)
                    elif header["type"] == "completed":
                        result = deserialize_rollout_payload(payload, rollout_id)
                        pending.terminal = True
                        pending.future.set_result(result)
                    elif header["type"] == "error":
                        error = rollout_error_from_payload(payload, rollout_id)
                        if error.code == "ServerDraining":
                            self._mark_draining(connection)
                        pending.terminal = True
                        pending.future.set_exception(error)
                    else:
                        raise ValueError("Execute Rollout returned an unknown message type")
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    self._stop_admission()
                    pending.future.set_exception(exc)
        except (ConnectionClosed, OSError, EOFError) as exc:
            connection.expires = 0
            self._mark_draining(connection)
            for rollout_id, pending in connection.pending.items():
                if not pending.future.done():
                    if self._closed:
                        pending.future.set_exception(_RolloutConnectionLost(str(exc)))
                    else:
                        self._recoveries[rollout_id] = pending
            if self._recoveries and not self._closed and self._recovery_worker is None:
                self._recovery_worker = asyncio.create_task(self._recover_pending())
                self._recovery_worker.add_done_callback(self._report_recovery_failure)
            self._retire_connection(connection)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            connection.expires = 0
            for pending in connection.pending.values():
                if not pending.future.done():
                    self._stop_admission()
                    pending.future.set_exception(exc)

    def _report_recovery_failure(self, worker: asyncio.Task) -> None:
        if worker.cancelled():
            return
        failure = worker.exception()
        if failure is not None:
            _LOGGER.error("Shared Execute Rollout recovery failed.", exc_info=failure)
            self._stop_admission()
            for pending in self._recoveries.values():
                if not pending.future.done():
                    pending.future.set_exception(failure)

    async def _recovery_get(self, rollout_id: str, result: bool, limit: int) -> Optional[bytes]:
        client = self._operations._pipeline_client  # pylint: disable=protected-access
        config = self._operations._config  # pylint: disable=protected-access
        path = f"/rl_environments/rollouts/{quote(rollout_id, safe='')}"
        headers = {"Accept": "application/json"}
        finetune_token = await self._get_finetune_token()
        if finetune_token:
            headers["x-ms-finetune-identity"] = finetune_token.token
        request = HttpRequest(
            "GET",
            path + ("/result" if result else ""),
            params={"api-version": _RECOVERY_API_VERSION},
            headers=headers,
        )
        request.url = client.format_url(request.url, endpoint=config.endpoint)
        pipeline_response = await client._pipeline.run(  # pylint: disable=protected-access
            request, stream=True, retry_total=0
        )
        response = pipeline_response.http_response
        try:
            if response.status_code in _RECOVERY_RETRY_STATUSES:
                _LOGGER.debug(
                    "Rollout %s recovery lookup returned HTTP %s.", rollout_id, response.status_code
                )
                return None
            if response.status_code != 200:
                raise RLERolloutError(
                    response, code="RolloutRecoveryHttpError",
                    message=f"Recovery lookup returned HTTP {response.status_code}.",
                    rollout_id=rollout_id,
                )
            payload = bytearray()
            async for chunk in response.iter_bytes():
                if len(payload) + len(chunk) > limit:
                    raise ValueError("Rollout recovery response exceeded its response safety budget")
                payload.extend(chunk)
            return bytes(payload)
        finally:
            await response.close()

    async def _recover_one(self, rollout_id: str, pending: _Pending) -> Optional[RolloutResult]:
        payload = await self._recovery_get(rollout_id, False, 64 * 1024)
        if payload is None:
            return None
        status = json.loads(payload)
        if (
            not isinstance(status, Mapping)
            or status.get("rollout_id") != rollout_id
            or status.get("environment_name") != self._name
            or status.get("environment_version") != self._version
            or status.get("job_id") != self._job_id
        ):
            raise ValueError("Rollout recovery returned invalid or mismatched metadata")
        if status.get("status") == "failed":
            pending.terminal = True
            raise RLERolloutError(
                None, code="RolloutFailed",
                message="The service reports that the disconnected rollout failed.",
                rollout_id=rollout_id,
            )
        if status.get("status") == "completed":
            payload = await self._recovery_get(
                rollout_id, True, min(pending.limit, DEFAULT_MAX_RESPONSE_BYTES)
            )
            return deserialize_rollout_payload(payload, rollout_id) if payload is not None else None
        if status.get("status") != "running":
            raise ValueError("Rollout recovery returned an unknown rollout status")
        return None

    async def _poll_recovery(self, rollout_id: str, pending: _Pending) -> None:
        try:
            result = await asyncio.wait_for(
                self._recover_one(rollout_id, pending),
                timeout=min(30.0, max(0.0, pending.deadline - time.monotonic())),
            )
        except (asyncio.TimeoutError, ServiceRequestError, ServiceResponseError) as exc:
            _LOGGER.debug("Rollout %s recovery lookup failed: %s", rollout_id, exc)
        except (ValueError, RLERolloutError) as exc:
            if not pending.future.done():
                pending.future.set_exception(exc)
        else:
            if result is not None and not pending.future.done():
                pending.terminal = True
                pending.future.set_result(result)

    async def _recover_pending(self) -> None:
        _LOGGER.warning("Execute Rollout socket disconnected; starting shared HTTP recovery.")
        tasks: list[asyncio.Task] = []
        try:
            while self._recoveries and not self._closed:
                for rollout_id, pending in tuple(self._recoveries.items()):
                    if pending.future.done():
                        self._recoveries.pop(rollout_id, None)
                if not self._recoveries:
                    return
                await asyncio.sleep(_RECOVERY_POLL_INTERVAL)
                batch = [
                    (rollout_id, pending) for rollout_id, pending in self._recoveries.items()
                    if not pending.future.done()
                ]
                tasks = []
                for rollout_id, pending in batch:
                    pending.lookup = asyncio.create_task(self._poll_recovery(rollout_id, pending))
                    tasks.append(pending.lookup)
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                for (_, pending), outcome in zip(batch, outcomes):
                    if isinstance(outcome, BaseException) and not pending.future.done():
                        pending.future.set_exception(outcome)
        finally:
            if self._recovery_worker is asyncio.current_task():
                self._recovery_worker = None
            for lookup in tasks:
                lookup.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute_rollout(
        self,
        *,
        task: Any,
        policy: RolloutPolicy,
        sampling: Optional[SamplingOptions] = None,
        harness: Optional[HarnessConfiguration] = None,
        rollout_id: Optional[str] = None,
        timeout: float = DEFAULT_ROLLOUT_TIMEOUT_S,
        progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
        max_response_bytes: Optional[int] = None,
    ) -> RolloutResult:
        """Execute with a default 1860-second timeout starting after SDK admission.

        V3 and default V4 sessions wait through durable acceptance and worker retries,
        then return the full typed result. Disconnection resumes by attachment with
        the last received durable cursor; it never replays execution. The timeout
        and admission permit also cover bounded recovery and result retrieval.
        The HTTP recovery behavior below applies to legacy sessions only.

        Queue waiting does not consume the timeout, but is cancellable and wakes
        on close or a session-level protocol failure. The deadline then includes renewal,
        credentials, handshake, send serialization and execution. Submitted
        timeout/cancellation raises an unknown outcome with the original ID. A
        disconnected socket's pending rollouts use HTTP status/result recovery
        within the same deadline; execution is never replayed. An unknown outcome
        does not prevent this client from admitting subsequent rollouts.

        :keyword task: Task record passed to the environment's reset.
        :paramtype task: any
        :keyword policy: Policy used for this rollout.
        :paramtype policy: ~azure.ai.projects.rle.RolloutPolicy
        :keyword sampling: Optional sampling configuration.
        :paramtype sampling: ~azure.ai.projects.rle.SamplingOptions or None
        :keyword harness: Optional per-request harness override.
        :paramtype harness: ~azure.ai.projects.rle.HarnessConfiguration or None
        :keyword rollout_id: Optional original identifier; generated when omitted.
        :paramtype rollout_id: str or None
        :keyword timeout: Post-admission timeout in seconds. Default 1860.
        :paramtype timeout: float
        :keyword progress_callback: Lightweight synchronous callback receiving correlated progress.
        :paramtype progress_callback: callable or None
        :keyword max_response_bytes: Positive response safety budget in bytes. Defaults to 32 MiB for V4
            HTTP result downloads and 8 MiB otherwise. None selects the protocol default. Larger V4
            HTTP downloads can be enabled by increasing this value; WebSocket payloads remain capped at 8 MiB.
        :paramtype max_response_bytes: int or None
        :return: The completed rollout.
        :rtype: ~azure.ai.projects.rle.RolloutResult
        :raises ~azure.ai.projects.operations.RLERolloutUnknownOutcomeError: Submitted outcome cannot be determined.
        """
        if max_response_bytes is None:
            max_response_bytes = self._default_max_response_bytes
        validate_rollout_options(timeout, max_response_bytes)
        body = build_rollout_body(
            task=task, policy=policy, sampling=sampling, rollout_id=rollout_id, job_id=self._job_id,
            harness=harness,
        )
        self._check()
        if self._queued is not None:
            return await self._queued.execute(body, timeout, progress_callback, max_response_bytes)
        async with self._lease(self._admission, None):
            deadline = time.monotonic() + timeout
            frame = serialize_rollout_frame("execute", body)
            attempt = 0
            while True:
                attempt += 1
                pending: Optional[_Pending] = None
                connection: Optional[_Connection] = None
                attempted = False
                try:
                    async with self._lease(self._send_lock, deadline):
                        connection = await self._wait_or_close(
                            asyncio.create_task(self._connect(deadline)),
                            deadline,
                            admission=True,
                        )
                        self._check()
                        if time.monotonic() >= deadline:
                            raise asyncio.TimeoutError()
                        if connection.draining:
                            raise RLERolloutError(
                                None, code="ServerDraining", message="Connection drained before submission.",
                                rollout_id=body.rollout_id,
                            )
                        if time.monotonic() >= connection.expires:
                            raise RLERolloutError(
                                None, code="RolloutConnectionClosed", message="Connection closed before submission."
                            )
                        if body.rollout_id in self._pending_ids:
                            raise ValueError("rollout_id is already pending in this session")
                        pending = _Pending(progress_callback, max_response_bytes, deadline)
                        self._pending_ids.add(body.rollout_id)
                        connection.pending[body.rollout_id] = pending
                        connection.replaceable.clear()
                        # Without an admission ACK, even an interrupted send is ambiguous.
                        attempted = True
                        await self._wait_or_close(asyncio.create_task(connection.socket.send(frame)), deadline)
                    return await self._wait_or_close(pending.future, deadline)
                except BaseException as exc:
                    if pending is not None and pending.terminal:
                        failure = pending.future.exception()
                        if not isinstance(failure, RLERolloutError) or failure.code != "ServerDraining":
                            return pending.future.result()
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        if attempt >= 3:
                            raise failure
                    elif attempted:
                        # Preserve callback/protocol errors instead of swallowing them.
                        if pending is not None and pending.future.done() and not pending.future.cancelled():
                            failure = pending.future.exception()
                            if failure is not None and not isinstance(failure, _RolloutConnectionLost):
                                self._stop_admission()
                                if failure is exc:
                                    raise
                                raise failure from exc
                        if (
                            connection is not None
                            and isinstance(exc, (ConnectionClosed, OSError, EOFError))
                            and not isinstance(exc, asyncio.TimeoutError)
                        ):
                            connection.expires = 0
                            self._mark_draining(connection)
                        raise RLERolloutUnknownOutcomeError(
                            rollout_id=body.rollout_id,
                            message=(
                                "Submitted rollout outcome is unknown after send failure, "
                                "cancellation, timeout, closure or unsuccessful disconnect recovery."
                            ),
                        ) from exc
                    elif isinstance(exc, RLERolloutError):
                        if exc.code != "ServerDraining" or attempt >= 3:
                            raise
                    else:
                        raise
                finally:
                    if pending is not None and connection is not None:
                        self._pending_ids.discard(body.rollout_id)
                        connection.pending.pop(body.rollout_id, None)
                        if not pending.future.done():
                            pending.future.cancel()
                        elif not pending.future.cancelled():
                            pending.future.exception()
                        if pending.lookup is not None:
                            pending.lookup.cancel()
                            await asyncio.gather(pending.lookup, return_exceptions=True)
                        self._recoveries.pop(body.rollout_id, None)
                        if not self._recoveries and self._recovery_worker is not None:
                            worker = self._recovery_worker
                            self._recovery_worker = None
                            worker.cancel()
                            await asyncio.gather(worker, return_exceptions=True)
                        if not connection.pending:
                            connection.replaceable.set()
                            if connection.draining:
                                self._retire_connection(connection)
                await self._wait_or_close(asyncio.create_task(asyncio.sleep(1)), deadline, admission=True)

    async def close(self) -> None:
        """Awaken queued callers and close local resources, not server execution."""
        if self._closed:
            return
        self._closed_event.set()
        self._admission_stopped.set()
        try:
            if self._queued is not None:
                await self._queued.close()
            if self._recovery_worker is not None:
                self._recovery_worker.cancel()
                await asyncio.gather(self._recovery_worker, return_exceptions=True)
            connections = tuple(self._connections)
            for connection in connections:
                for pending in connection.pending.values():
                    if not pending.future.done():
                        pending.future.set_exception(_RolloutConnectionLost("Rollout session closed."))
            results = await asyncio.gather(
                *(self._retire_connection(connection) for connection in connections), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            self._active = None
