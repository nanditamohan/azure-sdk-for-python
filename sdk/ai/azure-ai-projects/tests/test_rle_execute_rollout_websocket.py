# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Async shared-session tests; no live RLE service is required."""

import asyncio
import inspect
import json
import logging
import struct
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import pytest
from websockets.asyncio.server import serve
from azure.core.credentials import AccessToken

from azure.ai.projects.aio import AIProjectClient as AsyncAIProjectClient
from azure.ai.projects.aio.operations import _patch_rle_async as transport
from azure.ai.projects.aio.operations import _rollout_client_async as session_module
from azure.ai.projects.aio.operations._rollout_client_async import AsyncRolloutClient
from azure.ai.projects.operations import RLEOperations, RLERolloutError, RLERolloutUnknownOutcomeError
from azure.ai.projects.operations._patch_rle import _WEBSOCKET_LOGGER
from azure.ai.projects.operations._patch_rle_rollout import (
    DEFAULT_MAX_RESPONSE_BYTES,
    build_rollout_body,
    build_rollout_request,
    deserialize_rollout_frame,
    serialize_rollout_frame,
)
from azure.ai.projects.rle import LoomPolicy


class _Credential:
    def __init__(self):
        self.calls = 0
        self.lifetime = 3600

    async def get_token(self, *_args, **_kwargs):
        self.calls += 1
        return AccessToken(f"token-{self.calls}", int(time.time()) + self.lifetime)

    async def close(self):
        pass


class _Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.closed = False
        self.fail_send = False

    async def send(self, frame):
        self.sent.put_nowait(deserialize_rollout_frame(frame, DEFAULT_MAX_RESPONSE_BYTES))
        if self.fail_send:
            raise OSError("The send may already have reached the server.")

    async def recv(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True


def _policy():
    return LoomPolicy(
        model_name="model",
        project_endpoint="https://account.test/api/projects/project",
        session_id="session",
        checkpoint_id="checkpoint",
    )


def _frame(kind, rollout_id=None, payload=None):
    header = {"type": kind}
    if rollout_id is not None:
        header["rollout_id"] = rollout_id
    raw = json.dumps(header).encode()
    return struct.pack(">I", len(raw)) + raw + json.dumps(payload or {}).encode()


def _complete(socket, rollout_id):
    socket.incoming.put_nowait(_frame("completed", rollout_id, {"rollout_id": rollout_id, "reward": 1.0}))


def test_http_request_preserves_harness_override():
    harness = {"type": "gym"}
    body = build_rollout_body(task={"row": 1}, policy=_policy(), harness=harness)
    harness["type"] = "changed"
    request = build_rollout_request(
        environment_name="environment", environment_version="1", body=body, forwarded_token="test-token"
    )
    assert json.loads(request.content)["harness"] == {"type": "gym"}


def _session(monkeypatch, sockets, limit=16, **options):
    credential = _Credential()
    config = SimpleNamespace(
        endpoint="https://account.test/api/projects/project",
        credential=credential,
        credential_scopes=("https://ai.azure.com/.default",),
    )
    calls = []

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return sockets[len(calls) - 1]

    monkeypatch.setattr(transport, "websocket_connect", connect)
    return AsyncRolloutClient(
        SimpleNamespace(_config=config), "environment", "1", "job", limit, **options
    ), credential, calls


async def _sent(socket):
    return await asyncio.wait_for(socket.sent.get(), timeout=1)


def test_request_limit_includes_prefix_header_and_payload():
    limit = 4 * 1024 * 1024
    body = build_rollout_body(task="", policy=_policy(), rollout_id="boundary")
    overhead = len(serialize_rollout_frame("execute", body))
    body = build_rollout_body(task="x" * (limit - overhead), policy=_policy(), rollout_id="boundary")
    assert len(serialize_rollout_frame("execute", body)) == limit
    body = build_rollout_body(task="x" * (limit - overhead + 1), policy=_policy(), rollout_id="boundary")
    with pytest.raises(ValueError, match="4 MiB total"):
        serialize_rollout_frame("execute", body)


@pytest.mark.asyncio
async def test_oversized_request_does_not_interrupt_valid_sibling(monkeypatch):
    socket = _Socket()
    session, _, calls = _session(monkeypatch, [socket])
    async with session:
        valid = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="valid"))
        await _sent(socket)
        with pytest.raises(ValueError, match="4 MiB total"):
            await session.execute_rollout(task="x" * (4 * 1024 * 1024), policy=_policy(), rollout_id="large")
        assert not session._uncertain
        assert not socket.closed
        assert socket.sent.empty()
        assert len(calls) == 1
        _complete(socket, "valid")
        await valid


@pytest.mark.asyncio
async def test_cancelled_close_still_closes_socket(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    rollout = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="done"))
    await _sent(socket)
    _complete(socket, "done")
    await rollout
    receiver_joined = asyncio.Event()

    async def receiver():
        try:
            await asyncio.Future()
        finally:
            receiver_joined.set()
            await asyncio.Future()

    session._active.receiver.cancel()
    await asyncio.gather(session._active.receiver, return_exceptions=True)
    session._active.receiver = asyncio.create_task(receiver())
    await asyncio.sleep(0)
    closing = asyncio.create_task(session.close())
    await receiver_joined.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert socket.closed
    assert session._active is None
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", [
    None,
    {"type": "gym"},
    {"type": "byoh", "endpoint": "https://account.test/api/projects/project/agents/agent", "protocol": "responses"},
    {"type": "byoh", "endpoint": "https://harness.example.test/run", "protocol": "rle-invocation"},
])
async def test_harness_override_is_preserved_in_execute_frame(monkeypatch, harness):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={"row": 1}, policy=_policy(), harness=harness))
        header, payload = await _sent(socket)
        body = json.loads(payload)
        assert header["job_id"] == body["job_id"] == "job"
        if harness is None:
            assert "harness" not in body
        else:
            assert body["harness"] == harness
        _complete(socket, header["rollout_id"])
        await task


@pytest.mark.asyncio
async def test_public_factory_is_lazy_and_legacy_http_defaults():
    credential = _Credential()
    client = AsyncAIProjectClient(
        endpoint="https://account.test/api/projects/project", credential=credential, allow_preview=True
    )
    async with client.rle.get_rollout_client("environment", "1", max_concurrent_rollouts=3) as session:
        assert isinstance(session, AsyncRolloutClient)
        assert credential.calls == 0
        assert session._active is None
        assert inspect.signature(client.rle.execute_rollout).parameters["transport"].default == "http"
    assert not hasattr(RLEOperations, "get_rollout_client")
    assert not hasattr(client.rle, "open_rollout_session")
    assert "transport" not in inspect.signature(RLEOperations.execute_rollout).parameters
    await client.close()


@pytest.mark.asyncio
async def test_shared_twenty_calls_sdk_admission_out_of_order_auth_and_job(monkeypatch):
    socket = _Socket()
    session, credential, calls = _session(monkeypatch, [socket], limit=3)
    progress = []
    async with session:
        tasks = [
            asyncio.create_task(
                session.execute_rollout(
                    task={"row": index},
                    policy=_policy(),
                    rollout_id=str(index),
                    progress_callback=progress.append,
                    max_response_bytes=10 * 1024 * 1024,
                )
            )
            for index in range(20)
        ]
        handled = 0
        while handled < 20:
            batch = [await _sent(socket) for _ in range(min(3, 20 - handled))]
            assert socket.sent.empty()
            for header, payload in reversed(batch):
                assert header["job_id"] == "job"
                assert json.loads(payload)["job_id"] == "job"
                socket.incoming.put_nowait(_frame("progress", header["rollout_id"], {"stage": "running"}))
                _complete(socket, header["rollout_id"])
            handled += len(batch)
        results = await asyncio.gather(*tasks)
        assert [result.rollout_id for result in results] == [str(index) for index in range(20)]
        assert len(progress) == 20
        assert all("rollout_id" in update for update in progress)
        assert credential.calls == len(calls) == 1
        url, options = calls[0]
        assert "/instance_groups/_execute_rollout/instances/" in url
        assert "api-version=2025-11-15-preview" in url
        assert options["additional_headers"] == {"Authorization": "Bearer token-1", "aml-user-token": "token-1"}
        assert options["max_size"] == DEFAULT_MAX_RESPONSE_BYTES + 4100
    assert socket.closed


@pytest.mark.asyncio
async def test_draining_control_does_not_create_another_socket(monkeypatch):
    old = _Socket()
    session, credential, calls = _session(monkeypatch, [old], limit=2)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(old)
        await _sent(old)
        old.incoming.put_nowait(_frame("server-draining"))
        third = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="third"))
        await asyncio.sleep(0)
        assert len(calls) == 1
        _complete(old, "first")
        assert (await first).rollout_id == "first"
        assert (await _sent(old))[0]["rollout_id"] == "third"
        assert not old.closed
        _complete(old, "third")
        _complete(old, "second")
        await asyncio.gather(second, third)
        assert credential.calls == 1
        assert old.sent.empty()


@pytest.mark.asyncio
async def test_disconnect_is_unknown_without_replay_or_recovery(monkeypatch):
    socket = _Socket()
    session, _, calls = _session(monkeypatch, [socket], limit=2)
    async with session:
        tasks = [
            asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id=str(i))) for i in range(5)
        ]
        await _sent(socket)
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        assert [result.rollout_id for result in results[:2]] == ["0", "1"]
        assert all(isinstance(result, RLERolloutUnknownOutcomeError) for result in results[:2])
        assert all(isinstance(result, RLERolloutError) for result in results[2:])
        assert len(calls) == 1
        assert socket.sent.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["send", "timeout", "cancel"])
@pytest.mark.parametrize("server_renewal", [False, True])
async def test_submitted_unknown_blocks_new_work_and_preserves_id(monkeypatch, reason, server_renewal):
    socket = _Socket()
    socket.fail_send = reason == "send"
    session, _, calls = _session(
        monkeypatch, [socket], server_managed_token_renewal=server_renewal
    )
    async with session:
        task = asyncio.create_task(
            session.execute_rollout(
                task={},
                policy=_policy(),
                rollout_id="original",
                timeout=0.1 if reason == "timeout" else 1860,
            )
        )
        await _sent(socket)
        if reason == "cancel":
            task.cancel()
        with pytest.raises(RLERolloutUnknownOutcomeError) as error:
            await task
        assert error.value.rollout_id == "original"
        with pytest.raises(RLERolloutError) as blocked:
            await session.execute_rollout(task={}, policy=_policy(), rollout_id="new")
        assert blocked.value.code == "RolloutSessionUnaccounted"
        assert len(calls) == 1
        assert socket.sent.empty()


@pytest.mark.asyncio
async def test_queue_wait_does_not_consume_execution_timeout(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], limit=1)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(socket)
        queued = asyncio.create_task(
            session.execute_rollout(
                task={},
                policy=_policy(),
                rollout_id="queued",
                timeout=0.1,
            )
        )
        await asyncio.sleep(0.2)
        assert not queued.done()
        assert socket.sent.empty()
        _complete(socket, "first")
        await first
        assert (await _sent(socket))[0]["rollout_id"] == "queued"
        _complete(socket, "queued")
        assert (await queued).rollout_id == "queued"


@pytest.mark.asyncio
async def test_queue_cancellation_does_not_poison_admission(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], limit=1)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(socket)
        queued = asyncio.create_task(
            session.execute_rollout(
                task={},
                policy=_policy(),
                rollout_id="queued",
            )
        )
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        _complete(socket, "first")
        await first
        later = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="later"))
        assert (await _sent(socket))[0]["rollout_id"] == "later"
        _complete(socket, "later")
        await later


@pytest.mark.asyncio
async def test_cancelling_submitted_call_does_not_cancel_sibling(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], limit=2)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(socket)
        await _sent(socket)
        queued = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="queued"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(RLERolloutUnknownOutcomeError):
            await first
        assert not socket.closed
        with pytest.raises(RLERolloutError) as stopped:
            await asyncio.wait_for(queued, timeout=1)
        assert stopped.value.code == "RolloutSessionUnaccounted"
        _complete(socket, "second")
        assert (await second).rollout_id == "second"


@pytest.mark.asyncio
async def test_close_awakes_queue_and_stops_receiver(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], limit=1)
    submitted = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="submitted"))
    await _sent(socket)
    connection = session._active
    queued = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="queued"))
    await asyncio.sleep(0)
    await session.close()
    results = await asyncio.wait_for(asyncio.gather(submitted, queued, return_exceptions=True), timeout=1)
    assert isinstance(results[0], RLERolloutUnknownOutcomeError)
    assert results[0].rollout_id == "submitted"
    assert isinstance(results[1], RLERolloutError)
    assert results[1].code == "RolloutSessionClosed"
    assert connection.receiver.done()
    assert socket.closed
    assert session._active is None


@pytest.mark.asyncio
async def test_close_during_handshake_never_attaches_late_socket(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    started = asyncio.Event()

    async def late_connect(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return socket

    monkeypatch.setattr(transport, "websocket_connect", late_connect)
    task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="not-submitted"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await session.close()
    with pytest.raises(RLERolloutError):
        await asyncio.wait_for(task, timeout=1)
    assert socket.closed
    assert session._active is None
    assert socket.sent.empty()


@pytest.mark.asyncio
async def test_completion_already_received_wins_cancellation(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="complete"))
        await _sent(socket)
        pending = session._active.pending["complete"]
        pending.future.add_done_callback(lambda _: task.cancel())
        _complete(socket, "complete")
        assert (await task).rollout_id == "complete"
        assert not session._uncertain


@pytest.mark.asyncio
async def test_retryable_dependency_error_is_never_replayed(monkeypatch):
    socket = _Socket()
    session, _, calls = _session(monkeypatch, [socket])
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="failed"))
        await _sent(socket)
        socket.incoming.put_nowait(
            _frame(
                "error",
                "failed",
                {
                    "code": "RolloutDependencyFailed",
                    "message": "upstream failed",
                    "retryable": True,
                },
            )
        )
        with pytest.raises(RLERolloutError) as error:
            await task
        assert error.value.code == "RolloutDependencyFailed"
        assert error.value.rollout_id == "failed"
        assert not session._uncertain
        assert len(calls) == 1
        assert socket.sent.empty()


@pytest.mark.asyncio
async def test_callback_failure_is_not_swallowed(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])

    def fail(update):
        raise RuntimeError("callback failed")

    async with session:
        task = asyncio.create_task(
            session.execute_rollout(
                task={},
                policy=_policy(),
                rollout_id="callback",
                progress_callback=fail,
            )
        )
        await _sent(socket)
        socket.incoming.put_nowait(_frame("progress", "callback", {"stage": "running"}))
        with pytest.raises(RuntimeError, match="callback failed"):
            await task
        assert session._uncertain


@pytest.mark.asyncio
async def test_openenv_helper_retries_handshake_before_any_execute(monkeypatch):
    socket = _Socket()
    session, credential, _ = _session(monkeypatch, [socket])
    attempts = []

    async def connect(_url, **kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise OSError("transient handshake failure")
        return socket

    monkeypatch.setattr(transport, "websocket_connect", connect)
    monkeypatch.setattr(transport, "_websocket_retry_delay", lambda attempt: 0)
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="one-send"))
        await _sent(socket)
        assert len(attempts) == 3
        assert credential.calls == 1
        assert all(options["open_timeout"] <= 25 for options in attempts)
        _complete(socket, "one-send")
        await task
        assert socket.sent.empty()


@pytest.mark.asyncio
async def test_idle_disconnect_can_reconnect_without_replaying(monkeypatch):
    old, new = _Socket(), _Socket()
    session, credential, calls = _session(monkeypatch, [old, new])
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(old)
        _complete(old, "first")
        await first
        receiver = session._active.receiver
        old.incoming.put_nowait(OSError("idle disconnect"))
        await asyncio.wait_for(receiver, timeout=1)
        assert not session._uncertain
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        assert (await _sent(new))[0]["rollout_id"] == "second"
        assert old.closed
        assert len(calls) == credential.calls == 2
        assert old.sent.empty()
        _complete(new, "second")
        await second


@pytest.mark.asyncio
async def test_expiry_rotation_and_invalid_credential_are_pre_submission(monkeypatch):
    old, new = _Socket(), _Socket()
    session, credential, calls = _session(monkeypatch, [old, new])
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(old)
        session._active.expires = time.monotonic() - 1
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert not old.closed
        _complete(old, "first")
        await first
        await _sent(new)
        assert old.closed
        assert len(calls) == credential.calls == 2
        _complete(new, "second")
        await second
        session._active.expires = time.monotonic() - 1
        credential.lifetime = 30
        with pytest.raises(RLERolloutError) as error:
            await session.execute_rollout(task={}, policy=_policy(), rollout_id="not-submitted")
        assert error.value.code == "RolloutCredentialExpired"
        assert len(calls) == 2
        assert not session._uncertain


@pytest.mark.asyncio
async def test_server_renewal_keeps_socket_past_age_and_token_expiry(monkeypatch):
    socket = _Socket()
    session, credential, calls = _session(
        monkeypatch, [socket], server_managed_token_renewal=True
    )
    offset = 0
    monkeypatch.setattr(session_module, "time", SimpleNamespace(
        time=lambda: time.time() + offset,
        monotonic=lambda: time.monotonic() + offset,
    ))
    async with session:
        first = asyncio.create_task(session.execute_rollout(
            task={}, policy=_policy(), rollout_id="first", timeout=10800
        ))
        await _sent(socket)
        offset = 7200
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        assert (await _sent(socket))[0]["rollout_id"] == "second"
        assert not socket.closed
        assert len(calls) == credential.calls == 1
        _complete(socket, "second")
        _complete(socket, "first")
        await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_server_renewal_still_requires_valid_handshake_token(monkeypatch):
    session, credential, calls = _session(
        monkeypatch, [_Socket()], server_managed_token_renewal=True
    )
    credential.lifetime = 30
    async with session:
        with pytest.raises(RLERolloutError) as error:
            await session.execute_rollout(task={}, policy=_policy(), rollout_id="not-submitted")
        assert error.value.code == "RolloutCredentialExpired"
        assert not calls
        assert not session._uncertain


@pytest.mark.asyncio
async def test_server_renewal_reconnects_idle_socket_with_current_token(monkeypatch):
    old, new = _Socket(), _Socket()
    session, credential, calls = _session(
        monkeypatch, [old, new], server_managed_token_renewal=True
    )
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(old)
        _complete(old, "first")
        await first
        old.incoming.put_nowait(OSError("disconnected while idle"))
        await asyncio.wait_for(session._active.receiver, timeout=1)
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(new)
        assert old.closed
        assert len(calls) == credential.calls == 2
        assert calls[1][1]["additional_headers"]["aml-user-token"] == "token-2"
        _complete(new, "second")
        await second


@pytest.mark.parametrize(
    "header",
    [
        b'{"type":"completed","type":"error","rollout_id":"r"}',
        b'{"type":"completed"}',
        b" " * 4097,
    ],
)
def test_bounded_headers_and_duplicate_keys(header):
    with pytest.raises(ValueError):
        deserialize_rollout_frame(struct.pack(">I", len(header)) + header + b"{}", DEFAULT_MAX_RESPONSE_BYTES)


def test_caller_budget_does_not_relax_protocol_payload_bound():
    header = b'{"type":"completed","rollout_id":"r"}'
    with pytest.raises(ValueError):
        deserialize_rollout_frame(
            struct.pack(">I", len(header)) + header + b"x" * (DEFAULT_MAX_RESPONSE_BYTES + 1), 10 * 1024 * 1024
        )


def test_both_identity_headers_are_redacted(caplog):
    with caplog.at_level(logging.DEBUG, logger=_WEBSOCKET_LOGGER.logger.name):
        for name in ("Authorization", "aml-user-token"):
            _WEBSOCKET_LOGGER.debug("%s: %s", name, "secret-token")
    assert "secret-token" not in caplog.text


def _loopback_client(monkeypatch, server, limit=3):
    # Use the production URL builder and connection helper; only redirect the
    # destination to an unencrypted loopback server using a non-secret test token.
    port = server.sockets[0].getsockname()[1]
    original = session_module.build_rollout_websocket_url

    def loopback_url(*args):
        parsed = urlsplit(original(*args))
        return urlunsplit(("ws", f"127.0.0.1:{port}", parsed.path, parsed.query, ""))

    monkeypatch.setattr(session_module, "build_rollout_websocket_url", loopback_url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    credential = _Credential()
    client = AsyncAIProjectClient(
        endpoint="https://account.test/api/projects/project",
        credential=credential,
        allow_preview=True,
    )
    return client, client.rle.get_rollout_client("environment", "1", "job", limit), credential


@pytest.mark.asyncio
async def test_loopback_shared_socket_multiplexes_twenty_calls_and_closes(monkeypatch):
    connections = []
    closed = asyncio.Event()

    async def handler(socket):
        connections.append(socket)
        try:
            assert socket.request.headers["Authorization"] == "Bearer token-1"
            assert socket.request.headers["aml-user-token"] == "token-1"
            assert "/instance_groups/_execute_rollout/instances/" in socket.request.path
            assert socket.subprotocol == "rle.execute-rollout.v2"
            handled = 0
            while handled < 20:
                batch = [
                    deserialize_rollout_frame(await socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
                    for _ in range(min(3, 20 - handled))
                ]
                if handled == 0:
                    # A fourth packet must not be sent while the first three
                    # calls hold all SDK permits, even on the real transport.
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(socket.recv(), timeout=0.05)
                for header, payload in reversed(batch):
                    assert header["job_id"] == json.loads(payload)["job_id"] == "job"
                    identifier = header["rollout_id"]
                    await socket.send(_frame("progress", identifier, {"stage": "running"}))
                    await socket.send(_frame("completed", identifier, {"rollout_id": identifier, "reward": 1.0}))
                handled += len(batch)
            await socket.wait_closed()
        finally:
            closed.set()

    async with serve(handler, "127.0.0.1", 0, subprotocols=["rle.execute-rollout.v2"]) as server:
        client, session, credential = _loopback_client(monkeypatch, server)
        progress = []
        try:
            async with session:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            session.execute_rollout(
                                task={"row": index},
                                policy=_policy(),
                                rollout_id=str(index),
                                progress_callback=progress.append,
                                max_response_bytes=10 * 1024 * 1024,
                            )
                            for index in range(20)
                        )
                    ),
                    timeout=5,
                )
                receiver = session._active.receiver
                assert [result.rollout_id for result in results] == [str(index) for index in range(20)]
                assert len(progress) == 20
                assert credential.calls == len(connections) == 1
            await asyncio.wait_for(closed.wait(), timeout=1)
            assert receiver.done()
            assert session._active is None
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_loopback_handshake_retries_before_one_execution(monkeypatch):
    attempts = []
    received = []

    def process_request(socket, request):
        attempts.append(request)
        if len(attempts) < 3:
            return socket.respond(503, "Retry the opening handshake")
        return None

    async def handler(socket):
        header, _ = deserialize_rollout_frame(await socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
        received.append(header["rollout_id"])
        await socket.send(_frame("completed", header["rollout_id"], {"rollout_id": header["rollout_id"]}))
        await socket.wait_closed()

    monkeypatch.setattr(transport, "_websocket_retry_delay", lambda attempt: 0)
    async with serve(
        handler, "127.0.0.1", 0, process_request=process_request, subprotocols=["rle.execute-rollout.v2"]
    ) as server:
        client, session, credential = _loopback_client(monkeypatch, server)
        try:
            async with session:
                result = await asyncio.wait_for(
                    session.execute_rollout(task={}, policy=_policy(), rollout_id="original"), timeout=5
                )
                assert result.rollout_id == "original"
                assert len(attempts) == 3
                assert received == ["original"]
                assert credential.calls == 1
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_loopback_abrupt_disconnect_is_unknown_and_never_replayed(monkeypatch):
    received = []
    connections = []

    async def handler(socket):
        connections.append(socket)
        for _ in range(2):
            header, _ = deserialize_rollout_frame(await socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
            received.append(header["rollout_id"])
        socket.transport.abort()
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0, subprotocols=["rle.execute-rollout.v2"]) as server:
        client, session, _ = _loopback_client(monkeypatch, server, limit=2)
        try:
            async with session:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(session.execute_rollout(task={}, policy=_policy(), rollout_id=str(i)) for i in range(5)),
                        return_exceptions=True,
                    ),
                    timeout=5,
                )
                assert all(isinstance(result, RLERolloutUnknownOutcomeError) for result in results[:2])
                assert [result.rollout_id for result in results[:2]] == ["0", "1"]
                assert all(result.code == "RolloutSessionUnaccounted" for result in results[2:])
                assert received == ["0", "1"]
                assert len(connections) == 1
        finally:
            await client.close()
