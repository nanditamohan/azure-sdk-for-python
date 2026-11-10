# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""V4 completion-to-GET contract tests; no live service or local result files."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.aio.operations import _patch_rle_async as transport
from azure.ai.projects.aio.operations._rollout_client_async import AsyncRolloutClient
from azure.ai.projects.operations import RLERolloutError
from azure.ai.projects.operations._patch_rle_rollout import DEFAULT_MAX_RESPONSE_BYTES
from test_rle_execute_rollout_queued import (
    _QueuedSocket,
    _call,
    _command,
    _queued_session,
    _result,
    _result_pipeline,
    _running,
)
from test_rle_execute_rollout_websocket import _Credential, _RecoveryResponse, _frame, _policy, _sent

_PROTOCOL = "rle.execute-rollout.v4"


class _V4Socket(_QueuedSocket):
    subprotocol = _PROTOCOL


def _completion(sequence=1):
    return {"sequence": sequence, "attempt_id": "attempt", "result_available": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("use_factory", [False, True])
async def test_default_session_negotiates_v4_and_fetches_full_result(monkeypatch, use_factory):
    socket = _V4Socket()
    handshakes = []

    async def connect(*_args, **kwargs):
        handshakes.append(kwargs)
        return socket

    monkeypatch.setattr(transport, "websocket_connect", connect)
    async with AIProjectClient(
        "https://account.test/api/projects/project", _Credential(), allow_preview=True
    ) as client:
        session = (
            client.rle.get_rollout_client("environment", "1")
            if use_factory
            else AsyncRolloutClient(client.rle, "environment", "1")
        )
        requests, responses = _result_pipeline(session, lambda request: (200, _result(request.url.split("/")[-2])))
        async with session:
            call = _call(session)
            header, _ = await _sent(socket)
            socket.incoming.put_nowait(_frame("completed", header["rollout_id"], _completion()))
            result = await call
            assert result.rollout_id == header["rollout_id"]
            assert result.sequences[0]["input_ids"] == [1, 2]
            assert handshakes[0]["subprotocols"] == (_PROTOCOL,)
            assert len(requests) == 1 and responses[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("graph_field", ["rollout_graph", "rollout"])
async def test_real_v4_completion_fetches_full_typed_result_after_socket_closes(monkeypatch, large, graph_field):
    async with _running(monkeypatch, protocols=(_PROTOCOL,), protocol_version="v4") as run:

        def result(request):
            body = _result(request.url.split("/")[-2])
            body[graph_field] = body.pop("rollout")
            if large:
                body[graph_field]["extra"] = "x" * (DEFAULT_MAX_RESPONSE_BYTES + 1024)
            body["future_field"] = {"retained": True}
            return 200, body

        requests, responses = _result_pipeline(run.session, result)
        call = _call(run.session)
        socket, header, _ = await _command(run)
        rollout_id = header["rollout_id"]
        assert socket.subprotocol == _PROTOCOL
        assert run.requests[0].headers["Sec-WebSocket-Protocol"] == _PROTOCOL
        await socket.send(_frame("completed", rollout_id, _completion()))
        await socket.close()
        completed = await call
        assert completed.rollout_id == rollout_id
        assert completed.sequences[0]["input_ids"] == [1, 2]
        assert completed.sequences[0]["loss_mask"] == [0, 1]
        assert completed.sequences[0]["logprobs"] == [0.0, -0.5]
        assert getattr(completed, graph_field) == completed.as_dict()[graph_field]
        assert completed.as_dict()["future_field"] == {"retained": True}
        if large:
            assert len(completed.as_dict()[graph_field]["extra"]) == DEFAULT_MAX_RESPONSE_BYTES + 1024
        assert len(requests) == 1 and responses[0].closed
        assert requests[0].method == "GET"
        assert requests[0].url.startswith(
            "https://account.test/api/projects/project/rl_environments/rollouts/" + rollout_id + "/result?"
        )
        assert run.user.calls == run.finetune.calls == 2
        assert requests[0].headers["aml-user-token"] == "token-2"
        assert requests[0].headers["x-ms-finetune-identity"] == "finetune-token-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,size,succeeds",
    [
        (None, 32 * 1024 * 1024, True),
        (None, 32 * 1024 * 1024 + 1, False),
        (40 * 1024 * 1024, 32 * 1024 * 1024 + 1, True),
        (1024, 1024, True),
        (1024, 1025, False),
    ],
)
async def test_v4_result_download_default_and_override_boundaries(monkeypatch, limit, size, succeeds):
    socket = _V4Socket()
    session, _, _ = _queued_session(monkeypatch, [socket], protocol_version="v4")

    def result(request):
        body = _result(request.url.split("/")[-2])
        body["padding"] = ""
        body["padding"] = "x" * (size - len(json.dumps(body).encode()))
        return 200, body

    requests, responses = _result_pipeline(session, result)
    async with session:
        call = _call(session, **({} if limit is None else {"max_response_bytes": limit}))
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        socket.incoming.put_nowait(_frame("completed", rollout_id, _completion()))
        if succeeds:
            assert (await call).rollout_id == rollout_id
        else:
            with pytest.raises(RLERolloutError) as caught:
                await call
            budget = 32 * 1024 * 1024 if limit is None else limit
            assert caught.value.code == "RolloutResultTooLarge"
            assert caught.value.rollout_id == rollout_id
            assert f"max_response_bytes={budget}" in str(caught.value)
            assert f"at least {size} bytes" in str(caught.value)
            assert f"max_response_bytes={2 * budget}" in str(caught.value)
            assert "execute_rollout" in str(caught.value)
            assert "rollout completed" in str(caught.value)
        assert len(requests) == 1 and responses[0].closed
        while not socket.sent.empty():
            command, _ = socket.sent.get_nowait()
            assert command["type"] == "detach"


@pytest.mark.asyncio
async def test_v4_get_does_not_block_other_completions(monkeypatch):
    socket = _V4Socket()
    session, _, _ = _queued_session(monkeypatch, [socket], protocol_version="v4")
    downloading, release = asyncio.Event(), asyncio.Event()
    first_id = None

    async def run(request, **_kwargs):
        rollout_id = request.url.split("/")[-2]
        if rollout_id == first_id:
            downloading.set()
            await release.wait()
        return SimpleNamespace(http_response=_RecoveryResponse(request, 200, _result(rollout_id)))

    session._operations._pipeline_client = SimpleNamespace(
        format_url=lambda path, **_kwargs: session._operations._config.endpoint + path,
        _pipeline=SimpleNamespace(run=run),
    )
    async with session:
        first = _call(session)
        header, _ = await _sent(socket)
        first_id = header["rollout_id"]
        socket.incoming.put_nowait(_frame("completed", first_id, _completion()))
        await asyncio.wait_for(downloading.wait(), 2)
        try:
            second = _call(session)
            header, _ = await _sent(socket)
            while header["type"] == "detach":
                header, _ = await _sent(socket)
            socket.incoming.put_nowait(_frame("completed", header["rollout_id"], _completion()))
            assert (await asyncio.wait_for(second, 2)).rollout_id == header["rollout_id"]
            assert not first.done()
        finally:
            release.set()
        assert (await first).rollout_id == first_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"sequence": 1, "attempt_id": "attempt"},
        {**_completion(), "result_available": False},
        {**_completion(), "result_available": 1},
        {**_completion(), "result": {}},
        {**_completion(), "result_url": "https://untrusted.test/result"},
        {**_completion(), "attempt_id": None},
        {**_completion(), "sequence": None},
    ],
)
async def test_v4_invalid_completions_never_fetch_or_reexecute(monkeypatch, payload):
    socket = _V4Socket()
    session, _, _ = _queued_session(monkeypatch, [socket], protocol_version="v4")
    requests, _ = _result_pipeline(session, lambda _: (200, {}))
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        socket.incoming.put_nowait(_frame("completed", header["rollout_id"], payload))
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "InvalidRolloutFrame"
        assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {}, "RolloutResultUnavailable"),
        (404, {}, "RolloutResultUnavailable"),
        (500, {}, "RolloutResultUnavailable"),
        (200, {"rollout_id": "other"}, "InvalidRolloutResult"),
        (200, b"not-json", "RolloutResultUnavailable"),
        (200, b"x" * 2048, "RolloutResultTooLarge"),
    ],
)
async def test_v4_result_failures_do_not_reexecute(monkeypatch, status, body, code):
    socket = _V4Socket()
    session, _, _ = _queued_session(monkeypatch, [socket], protocol_version="v4")
    requests, responses = _result_pipeline(session, lambda _: (status, body))
    async with session:
        call = _call(session, max_response_bytes=1024)
        header, _ = await _sent(socket)
        socket.incoming.put_nowait(_frame("completed", header["rollout_id"], _completion()))
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == code
        assert len(requests) == 1 and responses[0].closed
        while not socket.sent.empty():
            command, _ = socket.sent.get_nowait()
            assert command["type"] == "detach"


@pytest.mark.asyncio
async def test_v4_requires_v4_negotiation_without_legacy_fallback(monkeypatch):
    socket = _QueuedSocket()  # A V3 selection cannot satisfy a V4 request.
    session, _, calls = _queued_session(monkeypatch, [socket], protocol_version="v4")
    async with session:
        with pytest.raises(RLERolloutError) as caught:
            await session.execute_rollout(task={}, policy=_policy())
        assert caught.value.code == "QueuedProtocolRequired"
        assert calls[0][1]["subprotocols"] == (_PROTOCOL,)
        assert socket.sent.empty()


@pytest.mark.asyncio
async def test_v4_reconnect_attaches_original_id_then_fetches_committed_result(monkeypatch):
    async with _running(monkeypatch, protocols=(_PROTOCOL,), protocol_version="v4") as run:
        requests, _ = _result_pipeline(run.session, lambda request: (200, _result(request.url.split("/")[-2])))
        call = _call(run.session)
        socket, header, _ = await _command(run)
        rollout_id = header["rollout_id"]
        await socket.send(
            _frame("progress", rollout_id, {"sequence": 0, "attempt_id": "first", "progress": {"phase": "running"}})
        )
        await socket.close()
        replacement, recovered, cursor = await _command(run, "attach")
        assert recovered["rollout_id"] == rollout_id
        assert cursor == {"last_sequence": 0}
        assert replacement.subprotocol == _PROTOCOL
        await replacement.send(_frame("completed", rollout_id, _completion(5)))
        assert (await call).rollout_id == rollout_id
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_v4_result_download_retains_original_deadline(monkeypatch):
    socket = _V4Socket()
    session, _, _ = _queued_session(monkeypatch, [socket], protocol_version="v4")
    downloading, cancelled = asyncio.Event(), asyncio.Event()

    async def run(_request, **_kwargs):
        downloading.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    session._operations._pipeline_client = SimpleNamespace(
        format_url=lambda path, **_kwargs: session._operations._config.endpoint + path,
        _pipeline=SimpleNamespace(run=run),
    )
    async with session:
        call = _call(session, timeout=0.2)
        header, _ = await _sent(socket)
        socket.incoming.put_nowait(_frame("completed", header["rollout_id"], _completion()))
        await asyncio.wait_for(downloading.wait(), 2)
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "RolloutResultUnavailable"
        assert cancelled.is_set()
