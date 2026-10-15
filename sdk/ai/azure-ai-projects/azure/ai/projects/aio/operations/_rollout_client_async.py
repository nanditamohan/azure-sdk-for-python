# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Execute Rollout client on the existing OpenEnv WebSocket transport."""

from __future__ import annotations

import asyncio  # pylint: disable=do-not-import-asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping, Optional

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
from ...rle._rollout_models import RolloutPolicy, RolloutResult, SamplingOptions


class _Pending:
    def __init__(self, callback: Any, limit: int) -> None:
        self.future = asyncio.get_running_loop().create_future()
        self.callback = callback
        self.limit = limit
        self.terminal = False


class _Connection:
    def __init__(self, socket: Any, expires: float) -> None:
        self.socket = socket
        self.expires = expires
        self.pending: dict[str, _Pending] = {}
        self.receiver: Optional[asyncio.Task] = None


class AsyncRolloutClient:
    """An event-loop-local shared session with one socket and SDK admission.

    The timeout starts after concurrency admission. By default, renewal waits for pending
    calls to settle before replacing the socket. Server-managed token renewal
    disables age/token-based replacement. Unknown submitted outcomes stop
    new admission, not siblings or server execution. There is no execution replay,
    durable recovery or drain-aware routing.
    """

    def __init__(
        self,
        operations: Any,
        environment_name: str,
        environment_version: str,
        job_id: Optional[str] = None,
        max_concurrent_rollouts: int = 16,
        *,
        server_managed_token_renewal: bool = False,
    ) -> None:
        validate_rollout_session(environment_name, environment_version, max_concurrent_rollouts)
        if not isinstance(server_managed_token_renewal, bool):
            raise ValueError("server_managed_token_renewal must be a bool")
        self._operations = operations
        self._server_managed_token_renewal = server_managed_token_renewal
        self._name = environment_name
        self._version = environment_version
        self._job_id = job_id
        self._admission = asyncio.Semaphore(max_concurrent_rollouts)
        self._send_lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active: Optional[_Connection] = None
        self._closed_event = asyncio.Event()
        self._admission_stopped = asyncio.Event()

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
        if connection.receiver is not None:
            connection.receiver.cancel()
            await asyncio.gather(connection.receiver, return_exceptions=True)
        try:
            await asyncio.wait_for(connection.socket.close(), timeout=5.0)
        except BaseException:
            transport = getattr(connection.socket, "transport", None)
            if transport is not None:
                transport.abort()
            raise

    async def _connect(self, deadline: float) -> _Connection:
        from ._patch_rle_async import _connect_openenv_websocket

        self._check()
        if self._active and time.monotonic() < self._active.expires:
            return self._active
        if self._active:
            await self._wait_or_close(asyncio.create_task(self._idle.wait()), deadline, admission=True)
            self._check()
            await self._close_connection(self._active)
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
        socket = await _connect_openenv_websocket(
            build_rollout_websocket_url(
                config.endpoint, self._name, self._version, uuid.uuid4().hex, EXECUTE_ROLLOUT_API_VERSION
            ),
            token=token.token,
            additional_headers={"aml-user-token": token.token},
            subprotocols=tuple(Subprotocol(value) for value in EXECUTE_ROLLOUT_SUBPROTOCOLS),
            timeout=max(0.0, deadline - time.monotonic()),
            max_size=DEFAULT_MAX_RESPONSE_BYTES + 4100,
            close_timeout=5.0,
        )
        expires = (
            float("inf")
            if self._server_managed_token_renewal
            else time.monotonic() + min(1740.0, token.expires_on - time.time() - 60.0)
        )
        connection = _Connection(socket, expires)
        if (
            self._closed or self._uncertain
            or time.monotonic() >= connection.expires
            or token.expires_on - time.time() <= 60
        ):
            await self._close_connection(connection)
            self._check()
            raise RLERolloutError(None, code="RolloutCredentialExpired", message="Token expired during the handshake.")
        self._active = connection
        connection.receiver = asyncio.create_task(self._receive(connection))
        return connection

    async def _receive(self, connection: _Connection) -> None:
        try:
            while not self._closed:
                header, payload = deserialize_rollout_frame(await connection.socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
                if header["type"] == "server-draining":
                    # Deployment drain routing is deliberately deferred in this first draft.
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
                        pending.terminal = True
                        pending.future.set_exception(error)
                    else:
                        raise ValueError("Execute Rollout returned an unknown message type")
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    self._stop_admission()
                    pending.future.set_exception(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            connection.expires = 0
            for pending in connection.pending.values():
                if not pending.future.done():
                    self._stop_admission()
                    pending.future.set_exception(
                        exc if isinstance(exc, ValueError) else _RolloutConnectionLost(str(exc))
                    )

    async def execute_rollout(
        self,
        *,
        task: Any,
        policy: RolloutPolicy,
        sampling: Optional[SamplingOptions] = None,
        harness: Optional[Mapping[str, Any]] = None,
        rollout_id: Optional[str] = None,
        timeout: float = DEFAULT_ROLLOUT_TIMEOUT_S,
        progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> RolloutResult:
        """Execute with a default 1860-second timeout starting after SDK admission.

        Queue waiting does not consume the timeout, but is cancellable and wakes
        on close or an unaccounted outcome. The deadline then includes renewal,
        credentials, handshake, send serialization and execution. Submitted
        timeout/cancellation/disconnect raises an unknown outcome with the original ID.

        :keyword task: Task record passed to the environment's reset.
        :paramtype task: any
        :keyword policy: Policy used for this rollout.
        :paramtype policy: ~azure.ai.projects.rle.RolloutPolicy
        :keyword sampling: Optional sampling configuration.
        :paramtype sampling: ~azure.ai.projects.rle.SamplingOptions or None
        :keyword harness: Optional per-request harness override.
        :paramtype harness: mapping[str, any] or None
        :keyword rollout_id: Optional original identifier; generated when omitted.
        :paramtype rollout_id: str or None
        :keyword timeout: Post-admission timeout in seconds. Default 1860.
        :paramtype timeout: float
        :keyword progress_callback: Lightweight synchronous callback receiving correlated progress.
        :paramtype progress_callback: callable or None
        :keyword max_response_bytes: Positive response safety budget; protocol payloads remain capped at 8 MiB.
        :paramtype max_response_bytes: int
        :return: The completed rollout.
        :rtype: ~azure.ai.projects.rle.RolloutResult
        :raises ~azure.ai.projects.operations.RLERolloutUnknownOutcomeError: Submitted outcome cannot be determined.
        """
        validate_rollout_options(timeout, max_response_bytes)
        body = build_rollout_body(
            task=task, policy=policy, sampling=sampling, rollout_id=rollout_id, job_id=self._job_id,
            harness=harness,
        )
        self._check()
        pending: Optional[_Pending] = None
        connection: Optional[_Connection] = None
        attempted = False
        async with self._lease(self._admission, None):
            deadline = time.monotonic() + timeout
            frame = serialize_rollout_frame("execute", body)
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
                    if time.monotonic() >= connection.expires:
                        raise RLERolloutError(
                            None, code="RolloutConnectionClosed", message="Connection closed before submission."
                        )
                    if body.rollout_id in connection.pending:
                        raise ValueError("rollout_id is already pending in this session")
                    pending = _Pending(progress_callback, max_response_bytes)
                    connection.pending[body.rollout_id] = pending
                    self._idle.clear()
                    # Without an admission ACK, even an interrupted send is ambiguous.
                    attempted = True
                    await self._wait_or_close(asyncio.create_task(connection.socket.send(frame)), deadline)
                return await self._wait_or_close(pending.future, deadline)
            except BaseException as exc:
                if pending is not None and pending.terminal:
                    return pending.future.result()
                if attempted:
                    self._stop_admission()
                    # Preserve callback/protocol errors instead of swallowing them.
                    if pending is not None and pending.future.done() and not pending.future.cancelled():
                        failure = pending.future.exception()
                        if failure is not None and not isinstance(failure, _RolloutConnectionLost):
                            if failure is exc:
                                raise
                            raise failure from exc
                    raise RLERolloutUnknownOutcomeError(
                        rollout_id=body.rollout_id,
                        message=(
                            "Submitted rollout outcome is unknown after send failure, "
                            "cancellation, timeout, closure or disconnect."
                        ),
                    ) from exc
                raise
            finally:
                if pending is not None and connection is not None:
                    connection.pending.pop(body.rollout_id, None)
                    if not pending.future.done():
                        pending.future.cancel()
                    elif not pending.future.cancelled():
                        pending.future.exception()
                    if not connection.pending:
                        self._idle.set()

    async def close(self) -> None:
        """Awaken queued callers and close local resources, not server execution."""
        if self._closed:
            return
        self._closed_event.set()
        self._admission_stopped.set()
        connection = self._active
        try:
            if connection is not None:
                for pending in connection.pending.values():
                    if not pending.future.done():
                        pending.future.set_exception(_RolloutConnectionLost("Rollout session closed."))
                await self._close_connection(connection)
        finally:
            self._active = None
