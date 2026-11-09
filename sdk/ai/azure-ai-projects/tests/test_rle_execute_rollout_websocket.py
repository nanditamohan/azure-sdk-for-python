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
from typing import Optional, get_type_hints
from urllib.parse import urlsplit, urlunsplit

import pytest
from websockets.asyncio.server import serve
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError

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
from azure.ai.projects.rle import HarnessConfiguration, LoomPolicy, SamplingOptions


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


def _recovery_status(rollout_id, status="completed"):
    return {
        "rollout_id": rollout_id,
        "job_id": "ftjob-test",
        "environment_name": "environment",
        "environment_version": "1",
        "status": status,
    }


class _RecoveryResponse:
    def __init__(self, request, status_code, body):
        self.request = request
        self.status_code = status_code
        self.reason = str(status_code)
        self.headers = {"content-type": "application/json"}
        self.payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.closed = False

    def json(self):
        return json.loads(self.payload)

    def text(self):
        return self.payload.decode()

    async def iter_bytes(self):
        for offset in range(0, len(self.payload), 16):
            yield self.payload[offset:offset + 16]

    async def close(self):
        self.closed = True


def _recovery_pipeline(session, responder):
    requests, responses = [], []

    async def run(request, **kwargs):
        assert request.method == "GET"
        assert kwargs == {"stream": True, "retry_total": 0}
        requests.append(request)
        reply = responder(request)
        if inspect.isawaitable(reply):
            reply = await reply
        if isinstance(reply, Exception):
            raise reply
        response = _RecoveryResponse(request, *reply)
        responses.append(response)
        return SimpleNamespace(http_response=response)

    session._operations._pipeline_client = SimpleNamespace(
        format_url=lambda path, **kwargs: session._operations._config.endpoint + path,
        _pipeline=SimpleNamespace(run=run),
    )
    return requests, responses


def _completed_recovery(request):
    parts = urlsplit(request.url).path.split("/")
    rollout_id = parts[-2] if parts[-1] == "result" else parts[-1]
    return 200, {"rollout_id": rollout_id, "reward": 1.0} if parts[-1] == "result" else _recovery_status(rollout_id)


def test_http_request_preserves_typed_harness_override():
    harness = HarnessConfiguration(type="gym")
    body = build_rollout_body(task={"row": 1}, policy=_policy(), harness=harness)
    request = build_rollout_request(
        environment_name="environment", environment_version="1", body=body, forwarded_token="test-token"
    )
    assert json.loads(request.content)["harness"] == {"type": "gym"}


def test_rollout_request_uses_typed_harness_with_job_correlation():
    body = build_rollout_body(
        task={"row": 1}, policy=_policy(), harness=HarnessConfiguration(type="gym"), job_id="job", rollout_id="rollout"
    )
    assert isinstance(body.harness, HarnessConfiguration)
    assert body.as_dict()["harness"] == {"type": "gym"}
    assert body.job_id == "job"
    assert body.rollout_id == "rollout"


@pytest.mark.parametrize("operation", [
    RLEOperations.execute_rollout,
    transport.RLEOperations.execute_rollout,
    AsyncRolloutClient.execute_rollout,
    build_rollout_body,
])
def test_rollout_operations_share_one_typed_harness_contract(operation):
    assert get_type_hints(operation)["harness"] == Optional[HarnessConfiguration]


def _session(monkeypatch, sockets, limit=16, job_id="ftjob-test", **options):
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
    monkeypatch.setattr(session_module, "_MAX_CONCURRENT_ROLLOUTS", limit)
    monkeypatch.setattr(session_module, "_RECOVERY_POLL_INTERVAL", 0)
    return AsyncRolloutClient(
        SimpleNamespace(_config=config), "environment", "1", job_id, **options
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
    HarnessConfiguration(type="gym"),
    HarnessConfiguration(
        type="byoh", endpoint="https://account.test/api/projects/project/agents/agent", protocol="responses"
    ),
    HarnessConfiguration(type="byoh", endpoint="https://harness.example.test/run", protocol="rle-invocation"),
    HarnessConfiguration(type="package", name="opencode"),
])
async def test_harness_override_is_preserved_in_execute_frame(monkeypatch, harness):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={"row": 1}, policy=_policy(), harness=harness))
        header, payload = await _sent(socket)
        body = json.loads(payload)
        assert header["job_id"] == body["job_id"] == "ftjob-test"
        if harness is None:
            assert "harness" not in body
        else:
            assert body["harness"] == harness.as_dict()
        _complete(socket, header["rollout_id"])
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("as_mapping", [False, True])
async def test_sampling_defaults_are_preserved_in_execute_frame(monkeypatch, as_mapping):
    values = {
        "max_completion_tokens": 32768, "temperature": 0,
        "top_p": 1, "top_k": -1, "seed": 0,
    }
    sampling = SamplingOptions(values) if as_mapping else SamplingOptions(**values)
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    async with session:
        task = asyncio.create_task(session.execute_rollout(
            task={"row": 1}, policy=_policy(), sampling=sampling,
        ))
        header, payload = await _sent(socket)
        assert json.loads(payload)["sampling"] == values
        _complete(socket, header["rollout_id"])
        await task


@pytest.mark.asyncio
async def test_public_factory_is_lazy_and_legacy_http_defaults():
    credential = _Credential()
    client = AsyncAIProjectClient(
        endpoint="https://account.test/api/projects/project", credential=credential, allow_preview=True
    )
    async with client.rle.get_rollout_client("environment", "1") as session:
        assert isinstance(session, AsyncRolloutClient)
        assert credential.calls == 0
        assert session._active is None
        assert inspect.signature(client.rle.execute_rollout).parameters["transport"].default == "http"
    assert not hasattr(RLEOperations, "get_rollout_client")
    assert not hasattr(client.rle, "open_rollout_session")
    assert "transport" not in inspect.signature(RLEOperations.execute_rollout).parameters
    await client.close()


@pytest.mark.parametrize("factory", [
    transport.RLEOperations.get_rollout_client,
    AsyncRolloutClient,
])
def test_rollout_concurrency_override_is_not_supported(factory):
    assert "max_concurrent_rollouts" not in inspect.signature(factory).parameters
    with pytest.raises(TypeError, match="max_concurrent_rollouts"):
        factory(SimpleNamespace(), "environment", "1", max_concurrent_rollouts=8)
    with pytest.raises(TypeError):
        factory(SimpleNamespace(), "environment", "1", None, 8)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_factory", [False, True])
@pytest.mark.parametrize("identifier", [None, "ftjob-test"])
async def test_optional_job_id_is_preserved_across_connections(
    monkeypatch, use_factory, identifier
):
    sockets = [_Socket(), _Socket()]
    handshakes = []

    async def connect(*_args, **kwargs):
        handshakes.append(kwargs["additional_headers"])
        return sockets[len(handshakes) - 1]

    monkeypatch.setattr(transport, "websocket_connect", connect)
    client = AsyncAIProjectClient(
        endpoint="https://account.test/api/projects/project", credential=_Credential(), allow_preview=True
    )
    session = (
        client.rle.get_rollout_client("environment", "1", job_id=identifier)
        if use_factory
        else AsyncRolloutClient(client.rle, "environment", "1", job_id=identifier)
    )
    try:
        async with session:
            for index, socket in enumerate(sockets):
                if index:
                    session._active.expires = 0
                task = asyncio.create_task(session.execute_rollout(task={"row": index}, policy=_policy()))
                try:
                    header, payload = await _sent(socket)
                    assert header.get("job_id") == json.loads(payload).get("job_id") == identifier
                    assert "x-ms-client-request-id" not in handshakes[index]
                    _complete(socket, header["rollout_id"])
                    await task
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_factory", [False, True])
async def test_sdk_managed_concurrency_admits_32_rollouts_and_queues_the_next(monkeypatch, use_factory):
    socket = _Socket()

    async def connect(*_args, **_kwargs):
        return socket

    monkeypatch.setattr(transport, "websocket_connect", connect)
    client = AsyncAIProjectClient(
        endpoint="https://account.test/api/projects/project", credential=_Credential(), allow_preview=True
    )
    session = (
        client.rle.get_rollout_client("environment", "1")
        if use_factory
        else AsyncRolloutClient(client.rle, "environment", "1")
    )
    try:
        async with session:
            tasks = [
                asyncio.create_task(session.execute_rollout(task={"row": index}, policy=_policy()))
                for index in range(33)
            ]
            try:
                batch = [await _sent(socket) for _ in range(32)]
                await asyncio.sleep(0.01)
                assert socket.sent.empty()
                assert not any(task.done() for task in tasks)

                _complete(socket, batch[0][0]["rollout_id"])
                last_header, _ = await _sent(socket)
                for header, _ in batch[1:]:
                    _complete(socket, header["rollout_id"])
                _complete(socket, last_header["rollout_id"])
                assert len(await asyncio.gather(*tasks)) == 33
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
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
                assert header["job_id"] == "ftjob-test"
                assert json.loads(payload)["job_id"] == "ftjob-test"
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
        assert options["additional_headers"] == {
            "Authorization": "Bearer token-1",
            "aml-user-token": "token-1",
        }
        assert options["max_size"] == DEFAULT_MAX_RESPONSE_BYTES + 4100
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("server_renewal", [False, True])
async def test_draining_handoff_keeps_old_results_and_global_admission(monkeypatch, server_renewal):
    old, new = _Socket(), _Socket()
    session, credential, calls = _session(
        monkeypatch, [old, new], limit=2, server_managed_token_renewal=server_renewal
    )
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(old)
        await _sent(old)
        connection = session._active
        old.incoming.put_nowait(_frame("server-draining", payload={"drain_timeout_seconds": 1800}))
        await asyncio.wait_for(connection.replaceable.wait(), timeout=1)
        old.incoming.put_nowait(_frame("server-draining"))
        third = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="third"))
        await asyncio.sleep(0)
        assert len(calls) == 1
        _complete(old, "first")
        assert (await first).rollout_id == "first"
        assert (await _sent(new))[0]["rollout_id"] == "third"
        assert not old.closed
        _complete(new, "third")
        _complete(old, "second")
        await asyncio.gather(second, third)
        await asyncio.wait_for(connection.closing, timeout=1)
        assert old.closed
        assert credential.calls == len(calls) == 2
        assert calls[1][1]["additional_headers"]["aml-user-token"] == "token-2"
        assert old.sent.empty()
    assert new.closed


@pytest.mark.asyncio
async def test_server_draining_rejection_resubmits_only_rejected_id(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new], limit=2)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="running"))
        await _sent(old)
        rejected = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="rejected"))
        original = await _sent(old)
        old.incoming.put_nowait(_frame("error", "rejected", {
            "code": "ServerDraining", "message": "Not admitted", "retryable": True,
        }))
        replacement = await asyncio.wait_for(new.sent.get(), timeout=2)
        assert replacement == original
        assert not old.closed
        assert not session._uncertain
        _complete(new, "rejected")
        _complete(old, "running")
        assert [result.rollout_id for result in await asyncio.gather(first, rejected)] == ["running", "rejected"]
        assert len(calls) == 2
        assert old.sent.empty()
        assert new.sent.empty()


@pytest.mark.asyncio
async def test_repeated_draining_rejections_are_bounded(monkeypatch):
    sockets = [_Socket() for _ in range(3)]
    session, _, calls = _session(monkeypatch, sockets)
    async with session:
        rollout = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="original"))
        for socket in sockets:
            assert (await asyncio.wait_for(socket.sent.get(), timeout=2))[0]["rollout_id"] == "original"
            socket.incoming.put_nowait(_frame("error", "original", {
                "code": "ServerDraining", "message": "Not admitted", "retryable": True,
            }))
        with pytest.raises(RLERolloutError) as error:
            await asyncio.wait_for(rollout, timeout=1)
        assert error.value.code == "ServerDraining"
        assert error.value.rollout_id == "original"
        assert not session._uncertain
        assert len(calls) == 3
    assert all(socket.closed for socket in sockets)


@pytest.mark.asyncio
async def test_drain_admission_retry_keeps_original_deadline(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new])
    async with session:
        rollout = asyncio.create_task(
            session.execute_rollout(task={}, policy=_policy(), rollout_id="original", timeout=0.1)
        )
        await _sent(old)
        old.incoming.put_nowait(_frame("error", "original", {
            "code": "ServerDraining", "message": "Not admitted",
        }))
        with pytest.raises(asyncio.TimeoutError):
            await rollout
        assert not session._uncertain
        assert len(calls) == 1
        assert new.sent.empty()


@pytest.mark.asyncio
async def test_drain_unblocks_connection_age_rotation_with_pending_work(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new], limit=2)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="old"))
        await _sent(old)
        connection = session._active
        connection.expires = 0
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
        await asyncio.sleep(0)
        assert len(calls) == 1
        old.incoming.put_nowait(_frame("server-draining"))
        await _sent(new)
        assert not old.closed
        _complete(new, "new")
        _complete(old, "old")
        await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_multiple_draining_sockets_preserve_correlation_and_close(monkeypatch):
    sockets = [_Socket() for _ in range(3)]
    session, _, calls = _session(monkeypatch, sockets, limit=3)
    tasks, connections = [], []
    async with session:
        for index, socket in enumerate(sockets):
            task = asyncio.create_task(
                session.execute_rollout(task={}, policy=_policy(), rollout_id=str(index))
            )
            tasks.append(task)
            await _sent(socket)
            connection = session._active
            connections.append(connection)
            if index < 2:
                socket.incoming.put_nowait(_frame("server-draining"))
                await asyncio.wait_for(connection.replaceable.wait(), timeout=1)
        assert len(calls) == 3
        assert not any(socket.closed for socket in sockets)
        with pytest.raises(ValueError, match="already pending"):
            # Release one global permit without completing the duplicated ID.
            _complete(sockets[2], "2")
            await tasks[2]
            await session.execute_rollout(task={}, policy=_policy(), rollout_id="0")
        _complete(sockets[1], "1")
        _complete(sockets[0], "0")
        assert [result.rollout_id for result in await asyncio.gather(*tasks)] == ["0", "1", "2"]
        assert not session._uncertain
    assert all(socket.closed for socket in sockets)
    assert all(connection.receiver.done() for connection in connections)
    assert not session._connections


@pytest.mark.asyncio
async def test_close_cleans_both_active_and_draining_sockets(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, _ = _session(monkeypatch, [old, new], limit=2)
    first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="old"))
    await _sent(old)
    connection = session._active
    old.incoming.put_nowait(_frame("server-draining"))
    await asyncio.wait_for(connection.replaceable.wait(), timeout=1)
    second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
    await _sent(new)
    await session.close()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(outcome, RLERolloutUnknownOutcomeError) for outcome in outcomes)
    assert [outcome.rollout_id for outcome in outcomes] == ["old", "new"]
    assert old.closed and new.closed
    assert not session._connections


@pytest.mark.asyncio
async def test_draining_disconnect_never_replays_old_work_or_cancels_new_sibling(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new], limit=2)
    requests, responses = _recovery_pipeline(session, _completed_recovery)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="old"))
        await _sent(old)
        connection = session._active
        old.incoming.put_nowait(_frame("server-draining"))
        await asyncio.wait_for(connection.replaceable.wait(), timeout=1)
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
        await _sent(new)
        old.incoming.put_nowait(OSError("drain deadline expired"))
        assert (await first).rollout_id == "old"
        _complete(new, "new")
        assert (await second).rollout_id == "new"
        assert not session._uncertain
        assert len(requests) == 2
        assert all("/rollouts/old" in request.url for request in requests)
        assert all(response.closed for response in responses)
        assert len(calls) == 2
        assert old.sent.empty() and new.sent.empty()


@pytest.mark.asyncio
async def test_idle_drain_closes_old_socket_before_next_submission(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new])
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(old)
        connection = session._active
        _complete(old, "first")
        await first
        old.incoming.put_nowait(_frame("server-draining"))
        await asyncio.wait_for(connection.receiver, timeout=1)
        await asyncio.wait_for(connection.closing, timeout=1)
        assert old.closed
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(new)
        _complete(new, "second")
        await second
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_replacement_handshake_does_not_interrupt_draining_results(monkeypatch):
    old = _Socket()
    session, _, _ = _session(monkeypatch, [old], limit=2)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="old"))
        await _sent(old)
        connection = session._active
        old.incoming.put_nowait(_frame("server-draining"))
        await asyncio.wait_for(connection.replaceable.wait(), timeout=1)

        async def fail(*_args, **_kwargs):
            raise OSError("replacement unavailable")

        monkeypatch.setattr(transport, "_connect_openenv_websocket", fail)
        with pytest.raises(OSError, match="replacement unavailable"):
            await session.execute_rollout(task={}, policy=_policy(), rollout_id="new")
        assert not session._uncertain
        assert not old.closed
        _complete(old, "old")
        assert (await first).rollout_id == "old"


@pytest.mark.asyncio
async def test_cancelled_replacement_handshake_preserves_old_rollout(monkeypatch):
    old = _Socket()
    session, _, _ = _session(monkeypatch, [old], limit=2)
    connecting = asyncio.Event()
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="old"))
        await _sent(old)
        connection = session._active
        old.incoming.put_nowait(_frame("server-draining"))
        await asyncio.wait_for(connection.replaceable.wait(), timeout=1)

        async def wait(*_args, **_kwargs):
            connecting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(transport, "_connect_openenv_websocket", wait)
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
        await asyncio.wait_for(connecting.wait(), timeout=1)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not session._uncertain
        assert not old.closed
        _complete(old, "old")
        assert (await first).rollout_id == "old"


@pytest.mark.asyncio
async def test_cancelling_known_drain_rejection_does_not_create_unknown_outcome(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new])
    async with session:
        rejected = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="rejected"))
        await _sent(old)
        connection = session._active
        old.incoming.put_nowait(_frame("error", "rejected", {
            "code": "ServerDraining", "message": "Not admitted", "retryable": True,
        }))
        await asyncio.wait_for(connection.replaceable.wait(), timeout=1)
        rejected.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rejected
        assert not session._uncertain
        assert len(calls) == 1
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
        await _sent(new)
        _complete(new, "new")
        assert (await second).rollout_id == "new"


@pytest.mark.asyncio
async def test_disconnect_recovers_pending_rollouts_without_replay(monkeypatch):
    socket, replacement = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [socket, replacement], limit=2)
    requests, responses = _recovery_pipeline(session, _completed_recovery)
    async with session:
        tasks = [
            asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id=str(i))) for i in range(2)
        ]
        await _sent(socket)
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert [result.rollout_id for result in results] == ["0", "1"]
        assert len(calls) == 1
        assert socket.sent.empty()
        assert len(requests) == 4 and all(response.closed for response in responses)
        assert not session._uncertain
        later = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="later"))
        assert (await _sent(replacement))[0]["rollout_id"] == "later"
        _complete(replacement, "later")
        await later
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", [None, "ftjob-test"])
async def test_recovery_preserves_optional_job_association(monkeypatch, job_id):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], job_id=job_id)

    def respond(request):
        code, body = _completed_recovery(request)
        if not urlsplit(request.url).path.endswith("/result") and job_id is None:
            body.pop("job_id")
        return code, body

    _recovery_pipeline(session, respond)
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="original"))
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        assert (await task).rollout_id == "original"
        assert not session._uncertain


@pytest.mark.asyncio
async def test_disconnect_does_not_reset_time_already_spent_executing(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    offset = 0
    monkeypatch.setattr(session_module, "time", SimpleNamespace(
        time=time.time,
        monotonic=lambda: time.monotonic() + offset,
    ))
    requests, _ = _recovery_pipeline(session, lambda request: (404, {}))
    async with session:
        task = asyncio.create_task(session.execute_rollout(
            task={}, policy=_policy(), rollout_id="original", timeout=10,
        ))
        await _sent(socket)
        offset = 9.99
        socket.incoming.put_nowait(OSError("disconnected after almost the entire deadline"))
        with pytest.raises(RLERolloutUnknownOutcomeError):
            await asyncio.wait_for(task, 0.5)
        assert requests and not session._uncertain


@pytest.mark.asyncio
async def test_recovery_uses_fixed_interval_and_retries_only_lookup_requests(monkeypatch):
    recovery_interval = session_module._RECOVERY_POLL_INTERVAL
    assert recovery_interval == 30
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    monkeypatch.setattr(session_module, "_RECOVERY_POLL_INTERVAL", recovery_interval)
    status_attempts, result_attempts, delays = [], [], []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))

    def respond(request):
        assert "api-version=2025-05-01" in request.url
        if urlsplit(request.url).path.endswith("/result"):
            result_attempts.append(request)
            if len(result_attempts) <= 2:
                return (404 if len(result_attempts) == 1 else 503), {}
            return _completed_recovery(request)
        status_attempts.append(request)
        attempt = len(status_attempts)
        if attempt <= 2:
            return (404 if attempt == 1 else 503), {}
        if attempt == 3:
            return ServiceRequestError("Temporary HTTP connection failure")
        return 200, _recovery_status("original", "running" if attempt == 4 else "completed")

    requests, responses = _recovery_pipeline(session, respond)
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="original"))
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        assert (await asyncio.wait_for(task, 1)).rollout_id == "original"
        assert delays == [30] * 7
        assert len(requests) == 10 and len(result_attempts) == 3
        assert all(response.closed for response in responses)
        assert socket.sent.empty() and not session._uncertain


@pytest.mark.asyncio
async def test_original_deadline_can_expire_before_first_recovery_round(monkeypatch):
    recovery_interval = session_module._RECOVERY_POLL_INTERVAL
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])
    monkeypatch.setattr(session_module, "_RECOVERY_POLL_INTERVAL", recovery_interval)
    requests, _ = _recovery_pipeline(session, _completed_recovery)
    async with session:
        task = asyncio.create_task(
            session.execute_rollout(task={}, policy=_policy(), rollout_id="original", timeout=0.1)
        )
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        with pytest.raises(RLERolloutUnknownOutcomeError):
            await asyncio.wait_for(task, 1)
        assert not requests and socket.sent.empty()
        assert not session._recoveries and not session._pending_ids
        assert session._recovery_worker is None and not session._uncertain


@pytest.mark.asyncio
async def test_successive_socket_losses_join_one_shared_polling_schedule(monkeypatch):
    recovery_interval = session_module._RECOVERY_POLL_INTERVAL
    sockets = [_Socket() for _ in range(4)]
    session, _, calls = _session(monkeypatch, sockets, limit=4)
    monkeypatch.setattr(session_module, "_RECOVERY_POLL_INTERVAL", recovery_interval)
    sleeping, advance = asyncio.Queue(), asyncio.Queue()
    delays = []
    round_number = 0

    async def sleep(delay):
        nonlocal round_number
        delays.append(delay)
        sleeping.put_nowait(delay)
        await advance.get()
        round_number += 1

    def respond(request):
        if round_number == 1:
            rollout_id = urlsplit(request.url).path.split("/")[-1]
            return 200, _recovery_status(rollout_id, "running")
        return _completed_recovery(request)

    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    requests, _ = _recovery_pipeline(session, respond)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(sockets[0])
        sockets[0].incoming.put_nowait(OSError("first socket lost"))
        assert await asyncio.wait_for(sleeping.get(), 1) == 30
        worker = session._recovery_worker

        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(sockets[1])
        connection = session._active
        sockets[1].incoming.put_nowait(OSError("second socket lost"))
        await asyncio.wait_for(connection.receiver, 1)
        assert session._recovery_worker is worker
        assert set(session._recoveries) == {"first", "second"}
        assert not requests and delays == [30]

        advance.put_nowait(None)
        assert await asyncio.wait_for(sleeping.get(), 1) == 30
        assert len(requests) == 2

        third = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="third"))
        await _sent(sockets[2])
        connection = session._active
        sockets[2].incoming.put_nowait(OSError("third socket lost"))
        await asyncio.wait_for(connection.receiver, 1)
        assert session._recovery_worker is worker
        assert set(session._recoveries) == {"first", "second", "third"}
        assert delays == [30, 30] and len(requests) == 2

        live = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="live"))
        assert (await _sent(sockets[3]))[0]["rollout_id"] == "live"
        _complete(sockets[3], "live")
        await live
        advance.put_nowait(None)
        results = await asyncio.wait_for(asyncio.gather(first, second, third), 1)
        assert [result.rollout_id for result in results] == ["first", "second", "third"]
        assert len(requests) == 8 and delays == [30, 30]
        assert all("/rollouts/live" not in request.url for request in requests)
        assert len(calls) == 4 and all(socket.sent.empty() for socket in sockets)
        assert not session._recoveries and session._recovery_worker is None

        later = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="later"))
        await _sent(sockets[3])
        sockets[3].incoming.put_nowait(OSError("a new recovery batch"))
        assert await asyncio.wait_for(sleeping.get(), 1) == 30
        assert session._recovery_worker is not worker
        advance.put_nowait(None)
        assert (await asyncio.wait_for(later, 1)).rollout_id == "later"
        assert delays == [30, 30, 30]


@pytest.mark.asyncio
async def test_socket_loss_during_a_poll_round_joins_the_next_round(monkeypatch):
    recovery_interval = session_module._RECOVERY_POLL_INTERVAL
    first_socket, second_socket = _Socket(), _Socket()
    session, _, _ = _session(monkeypatch, [first_socket, second_socket], limit=2)
    monkeypatch.setattr(session_module, "_RECOVERY_POLL_INTERVAL", recovery_interval)
    sleeping, advance = asyncio.Queue(), asyncio.Queue()
    polling, finish_lookup = asyncio.Event(), asyncio.Event()
    round_number = 0

    async def sleep(delay):
        nonlocal round_number
        sleeping.put_nowait(delay)
        await advance.get()
        round_number += 1

    async def respond(request):
        if round_number == 1:
            polling.set()
            await finish_lookup.wait()
            return 200, _recovery_status("first", "running")
        return _completed_recovery(request)

    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    requests, _ = _recovery_pipeline(session, respond)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(first_socket)
        first_socket.incoming.put_nowait(OSError("first socket lost"))
        assert await asyncio.wait_for(sleeping.get(), 1) == 30
        worker = session._recovery_worker
        advance.put_nowait(None)
        await asyncio.wait_for(polling.wait(), 1)

        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="second"))
        await _sent(second_socket)
        connection = session._active
        second_socket.incoming.put_nowait(OSError("second socket lost during HTTP lookup"))
        await asyncio.wait_for(connection.receiver, 1)
        assert session._recovery_worker is worker
        assert set(session._recoveries) == {"first", "second"} and len(requests) == 1
        finish_lookup.set()
        assert await asyncio.wait_for(sleeping.get(), 1) == 30
        advance.put_nowait(None)
        assert [result.rollout_id for result in await asyncio.gather(first, second)] == ["first", "second"]
        assert len(requests) == 5
        assert session._recovery_worker is None


@pytest.mark.asyncio
async def test_unexpected_shared_worker_failure_is_reported_to_callers(monkeypatch, caplog):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])

    async def fail(delay):
        raise RuntimeError("Recovery scheduler failed")

    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": fail}))
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="original"))
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        with pytest.raises(RuntimeError, match="Recovery scheduler failed"):
            await asyncio.wait_for(task, 1)
        assert "Shared Execute Rollout recovery failed" in caplog.text
        assert session._uncertain and not session._recoveries
        assert session._recovery_worker is None


@pytest.mark.asyncio
async def test_cancelling_one_shared_lookup_does_not_delay_sibling_completion(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], limit=2)
    blocked, cleaned = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if "/rollouts/blocked" in request.url:
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return _completed_recovery(request)

    _recovery_pipeline(session, respond)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="blocked"))
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="complete"))
        await _sent(socket)
        await _sent(socket)
        socket.incoming.put_nowait(OSError("shared socket lost"))
        await asyncio.wait_for(blocked.wait(), 1)
        assert (await asyncio.wait_for(second, 1)).rollout_id == "complete"
        assert not first.done() and session._recovery_worker is not None
        first.cancel()
        with pytest.raises(RLERolloutUnknownOutcomeError):
            await first
        assert cleaned.is_set()
        assert not session._recoveries and session._recovery_worker is None


@pytest.mark.asyncio
async def test_recovering_rollouts_keep_all_32_admission_slots(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new], limit=32)
    release = asyncio.Event()
    polling = asyncio.Event()
    count = 0

    async def respond(request):
        nonlocal count
        if not urlsplit(request.url).path.endswith("/result"):
            count += 1
            if count == 32:
                polling.set()
            await release.wait()
        return _completed_recovery(request)

    _recovery_pipeline(session, respond)
    async with session:
        tasks = [
            asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id=str(i)))
            for i in range(32)
        ]
        for _ in range(32):
            await _sent(old)
        queued = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="queued"))
        old.incoming.put_nowait(OSError("disconnected"))
        await asyncio.wait_for(polling.wait(), 1)
        assert len(calls) == 1 and not queued.done()
        assert new.sent.empty()
        release.set()
        assert (await _sent(new))[0]["rollout_id"] == "queued"
        _complete(new, "queued")
        results = await asyncio.gather(*tasks, queued)
        assert len(results) == 33 and len(calls) == 2
        assert not session._uncertain and not session._pending_ids


@pytest.mark.asyncio
async def test_recovery_does_not_block_new_work_or_allow_duplicate_pending_ids(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, new], limit=2)
    polling, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        polling.set()
        await release.wait()
        return _completed_recovery(request)

    requests, _ = _recovery_pipeline(session, respond)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="old"))
        await _sent(old)
        old.incoming.put_nowait(OSError("disconnected"))
        await asyncio.wait_for(polling.wait(), 1)
        with pytest.raises(ValueError, match="already pending"):
            await session.execute_rollout(task={}, policy=_policy(), rollout_id="old")
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
        assert (await _sent(new))[0]["rollout_id"] == "new"
        _complete(new, "new")
        assert (await second).rollout_id == "new"
        assert not first.done()
        release.set()
        assert (await first).rollout_id == "old"
        assert len(calls) == 2
        assert all("/rollouts/old" in request.url for request in requests)


@pytest.mark.asyncio
async def test_completion_received_before_disconnect_is_not_polled(monkeypatch):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket], limit=2)
    requests, _ = _recovery_pipeline(session, _completed_recovery)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="complete"))
        second = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="recover"))
        await _sent(socket)
        await _sent(socket)
        _complete(socket, "complete")
        socket.incoming.put_nowait(OSError("disconnected"))
        assert [result.rollout_id for result in await asyncio.gather(first, second)] == ["complete", "recover"]
        assert all("/rollouts/recover" in request.url for request in requests)


@pytest.mark.asyncio
async def test_recovered_failure_is_terminal_and_does_not_stop_admission(monkeypatch):
    old, new = _Socket(), _Socket()
    session, _, _ = _session(monkeypatch, [old, new])
    requests, _ = _recovery_pipeline(session, lambda request: (200, _recovery_status("failed", "failed")))
    async with session:
        task = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="failed"))
        await _sent(old)
        old.incoming.put_nowait(OSError("disconnected"))
        with pytest.raises(RLERolloutError) as error:
            await task
        assert error.value.code == "RolloutFailed" and error.value.rollout_id == "failed"
        assert len(requests) == 1 and not session._uncertain
        later = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="later"))
        await _sent(new)
        _complete(new, "later")
        await later


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["missing", "running", "missing_result", "blocked", "cancel", "close"])
async def test_unresolved_recovery_keeps_original_deadline_and_cleans_up(monkeypatch, reason):
    socket, replacement = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [socket, replacement], limit=1)
    polling, cleaned = asyncio.Event(), asyncio.Event()

    async def respond(request):
        polling.set()
        if reason in {"blocked", "cancel", "close"}:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        if reason == "missing" or urlsplit(request.url).path.endswith("/result"):
            return 404, {}
        return 200, _recovery_status("original", "running" if reason == "running" else "completed")

    _recovery_pipeline(session, respond)
    async with session:
        task = asyncio.create_task(session.execute_rollout(
            task={}, policy=_policy(), rollout_id="original", timeout=0.1,
        ))
        await _sent(socket)
        queued = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="queued"))
        socket.incoming.put_nowait(OSError("disconnected"))
        await asyncio.wait_for(polling.wait(), 1)
        if reason == "cancel":
            task.cancel()
        elif reason == "close":
            await session.close()
        with pytest.raises(RLERolloutUnknownOutcomeError) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.rollout_id == "original"
        if reason == "close":
            with pytest.raises(RLERolloutError) as closed:
                await queued
            assert closed.value.code == "RolloutSessionClosed"
        else:
            assert not session._uncertain
            assert (await _sent(replacement))[0]["rollout_id"] == "queued"
            _complete(replacement, "queued")
            assert (await queued).rollout_id == "queued"
        if reason in {"blocked", "cancel", "close"}:
            assert cleaned.is_set()
        assert len(calls) == (1 if reason == "close" else 2)
        assert socket.sent.empty() and replacement.sent.empty()
        assert not session._recoveries and not session._pending_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "metadata_id", "environment", "version", "job", "unknown_status", "json", "result_id", "size", "auth",
])
async def test_recovery_rejects_invalid_results_and_nonretryable_failures(monkeypatch, bad):
    socket = _Socket()
    session, _, _ = _session(monkeypatch, [socket])

    def respond(request):
        if bad == "auth":
            return 403, {"code": "Forbidden", "message": "Read access denied"}
        if urlsplit(request.url).path.endswith("/result"):
            return 200, {
                "rollout_id": "other" if bad == "result_id" else "original",
                "padding": "x" * (512 if bad == "size" else 0),
            }
        value = _recovery_status("original")
        for option, field in [
            ("metadata_id", "rollout_id"), ("environment", "environment_name"),
            ("version", "environment_version"), ("job", "job_id"), ("unknown_status", "status"),
        ]:
            if bad == option:
                value[field] = "unexpected"
        return 200, b"{" if bad == "json" else value

    requests, responses = _recovery_pipeline(session, respond)
    async with session:
        task = asyncio.create_task(session.execute_rollout(
            task={}, policy=_policy(), rollout_id="original", max_response_bytes=128,
        ))
        await _sent(socket)
        socket.incoming.put_nowait(OSError("disconnected"))
        with pytest.raises(RLERolloutError if bad == "auth" else ValueError):
            await asyncio.wait_for(task, 1)
        assert 1 <= len(requests) <= 2
        assert all(response.closed for response in responses)
        assert session._uncertain and socket.sent.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["send", "timeout", "cancel"])
@pytest.mark.parametrize("server_renewal", [False, True])
async def test_submitted_unknown_allows_new_work_and_preserves_id(monkeypatch, reason, server_renewal):
    socket, replacement = _Socket(), _Socket()
    socket.fail_send = reason == "send"
    session, _, calls = _session(
        monkeypatch, [socket, replacement], server_managed_token_renewal=server_renewal
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
        assert not session._uncertain
        later = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="new"))
        active_socket = replacement if reason == "send" else socket
        assert (await _sent(active_socket))[0]["rollout_id"] == "new"
        _complete(socket, "original")
        _complete(active_socket, "new")
        assert (await later).rollout_id == "new"
        assert len(calls) == (2 if reason == "send" else 1)
        assert socket.sent.empty() and replacement.sent.empty()


@pytest.mark.asyncio
async def test_interrupted_send_replaces_socket_without_losing_existing_rollout(monkeypatch):
    old, replacement = _Socket(), _Socket()
    session, _, calls = _session(monkeypatch, [old, replacement], limit=2)
    async with session:
        first = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="first"))
        await _sent(old)
        old.fail_send = True
        interrupted = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="unknown"))
        await _sent(old)
        with pytest.raises(RLERolloutUnknownOutcomeError) as error:
            await interrupted
        assert error.value.rollout_id == "unknown" and not first.done()
        later = asyncio.create_task(session.execute_rollout(task={}, policy=_policy(), rollout_id="later"))
        assert (await _sent(replacement))[0]["rollout_id"] == "later"
        _complete(replacement, "later")
        _complete(old, "first")
        assert [result.rollout_id for result in await asyncio.gather(first, later)] == ["first", "later"]
        assert len(calls) == 2 and old.sent.empty() and replacement.sent.empty()


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
        assert (await _sent(socket))[0]["rollout_id"] == "queued"
        _complete(socket, "queued")
        assert (await queued).rollout_id == "queued"
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


def _loopback_client(monkeypatch, server, limit=3, job_id="ftjob-test"):
    # Use the production URL builder and connection helper; only redirect the
    # destination to an unencrypted loopback server using a non-secret test token.
    port = server.sockets[0].getsockname()[1]
    original = session_module.build_rollout_websocket_url

    def loopback_url(*args):
        parsed = urlsplit(original(*args))
        return urlunsplit(("ws", f"127.0.0.1:{port}", parsed.path, parsed.query, ""))

    monkeypatch.setattr(session_module, "build_rollout_websocket_url", loopback_url)
    monkeypatch.setattr(session_module, "_MAX_CONCURRENT_ROLLOUTS", limit)
    monkeypatch.setattr(session_module, "_RECOVERY_POLL_INTERVAL", 0)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    credential = _Credential()
    client = AsyncAIProjectClient(
        endpoint="https://account.test/api/projects/project",
        credential=credential,
        allow_preview=True,
    )
    return client, client.rle.get_rollout_client("environment", "1", job_id), credential


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [None, "ftjob-test"])
async def test_loopback_shared_socket_multiplexes_twenty_calls_and_closes(monkeypatch, identifier):
    connections = []
    closed = asyncio.Event()

    async def handler(socket):
        connections.append(socket)
        try:
            assert socket.request.headers["Authorization"] == "Bearer token-1"
            assert socket.request.headers["aml-user-token"] == "token-1"
            assert "x-ms-client-request-id" not in socket.request.headers
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
                    assert header.get("job_id") == json.loads(payload).get("job_id") == identifier
                    rollout_id = header["rollout_id"]
                    await socket.send(_frame("progress", rollout_id, {"stage": "running"}))
                    await socket.send(_frame("completed", rollout_id, {"rollout_id": rollout_id, "reward": 1.0}))
                handled += len(batch)
            await socket.wait_closed()
        finally:
            closed.set()

    async with serve(handler, "127.0.0.1", 0, subprotocols=["rle.execute-rollout.v2"]) as server:
        client, session, credential = _loopback_client(monkeypatch, server, job_id=identifier)
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
async def test_loopback_drain_opens_replacement_before_original_completes(monkeypatch):
    connections, received = [], []
    draining = asyncio.Event()
    replacement_completed = asyncio.Event()

    async def handler(socket):
        connections.append(socket)
        index = len(connections)
        assert socket.request.headers["aml-user-token"] == f"token-{index}"
        header, _ = deserialize_rollout_frame(await socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
        rollout_id = header["rollout_id"]
        received.append(rollout_id)
        if index == 1:
            await socket.send(_frame("server-draining", payload={
                "drain_timeout_seconds": 1800, "retry_after_seconds": 1,
            }))
            draining.set()
            await replacement_completed.wait()
        await socket.send(_frame("progress", rollout_id, {"stage": "running"}))
        await socket.send(_frame("completed", rollout_id, {"rollout_id": rollout_id}))
        if index == 2:
            replacement_completed.set()
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0, subprotocols=["rle.execute-rollout.v2"]) as server:
        client, session, credential = _loopback_client(monkeypatch, server, limit=2)
        progress = []
        try:
            async with session:
                first = asyncio.create_task(session.execute_rollout(
                    task={}, policy=_policy(), rollout_id="old", progress_callback=progress.append
                ))
                await asyncio.wait_for(draining.wait(), timeout=2)
                await asyncio.wait_for(session._active.replaceable.wait(), timeout=1)
                second = asyncio.create_task(session.execute_rollout(
                    task={}, policy=_policy(), rollout_id="new", progress_callback=progress.append
                ))
                results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
                assert [result.rollout_id for result in results] == ["old", "new"]
                assert received == ["old", "new"]
                assert {update["rollout_id"] for update in progress} == {"old", "new"}
                assert credential.calls == len(connections) == 2
                assert not session._uncertain
            assert not session._connections
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
@pytest.mark.parametrize("http_status", [200, 403])
async def test_loopback_abrupt_disconnect_uses_authenticated_http_without_replay(monkeypatch, http_status):
    import gzip

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    received = []
    connections = []
    lookups = []

    async def handler(socket):
        connections.append(socket)
        if len(connections) == 1:
            for _ in range(2):
                header, _ = deserialize_rollout_frame(await socket.recv(), DEFAULT_MAX_RESPONSE_BYTES)
                received.append(header["rollout_id"])
            socket.transport.abort()
        else:
            async for message in socket:
                header, _ = deserialize_rollout_frame(message, DEFAULT_MAX_RESPONSE_BYTES)
                received.append(header["rollout_id"])
                await socket.send(_frame("completed", header["rollout_id"], {"rollout_id": header["rollout_id"]}))
        await socket.wait_closed()

    async def lookup(request):
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer token-")
        assert request.query["api-version"] == "2025-05-01"
        rollout_id = request.match_info["rollout_id"]
        assert rollout_id in {"0", "1"}
        lookups.append(request.path)
        if http_status != 200:
            return web.json_response({"code": "Forbidden", "message": "Read access denied"}, status=http_status)
        if request.path.endswith("/result"):
            return web.Response(
                body=gzip.compress(json.dumps({"rollout_id": rollout_id, "reward": 1.0}).encode()),
                headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            )
        return web.json_response(_recovery_status(rollout_id))

    app = web.Application()
    route = "/api/projects/project/rl_environments/rollouts/{rollout_id}"
    app.router.add_get(route, lookup)
    app.router.add_get(route + "/result", lookup)
    async with (
        serve(handler, "127.0.0.1", 0, subprotocols=["rle.execute-rollout.v2"]) as server,
        TestServer(app) as http,
    ):
        client, session, _ = _loopback_client(monkeypatch, server, limit=2)
        pipeline = client.rle._pipeline_client._pipeline
        original_run = pipeline.run

        async def run(request, **kwargs):
            parsed = urlsplit(request.url)
            request.url = str(http.make_url(parsed.path).with_query(parsed.query))
            return await original_run(request, enforce_https=False, **kwargs)

        monkeypatch.setattr(pipeline, "run", run)
        try:
            async with session:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(session.execute_rollout(task={}, policy=_policy(), rollout_id=str(i)) for i in range(5)),
                        return_exceptions=True,
                    ),
                    timeout=10,
                )
                if http_status == 200:
                    assert [result.rollout_id for result in results] == [str(i) for i in range(5)]
                    assert received == [str(i) for i in range(5)]
                    assert len(connections) == 2 and len(lookups) == 4
                    assert not session._uncertain
                else:
                    assert all(isinstance(result, RLERolloutError) for result in results)
                    assert all(result.code == "RolloutRecoveryHttpError" for result in results[:2])
                    assert all(result.status_code == 403 for result in results[:2])
                    assert all(result.code == "RolloutSessionUnaccounted" for result in results[2:])
                    assert received == ["0", "1"] and len(connections) == 1
                    assert len(lookups) == 2
        finally:
            await client.close()
