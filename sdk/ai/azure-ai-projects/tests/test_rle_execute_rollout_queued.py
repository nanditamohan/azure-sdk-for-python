# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Offline queued protocol contract tests using real loopback WebSockets."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from websockets.asyncio.server import serve

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.aio.operations import _patch_rle_async as transport
from azure.ai.projects.aio.operations import _queued_rollout_client_async as queued
from azure.ai.projects.operations import RLEOperations, RLERolloutError, RLERolloutUnknownOutcomeError
from azure.ai.projects.operations._patch_rle import _WEBSOCKET_LOGGER
from azure.ai.projects.operations._patch_rle_rollout import DEFAULT_MAX_RESPONSE_BYTES, deserialize_rollout_frame
from azure.ai.projects.rle import HarnessConfiguration, RolloutResult
from test_rle_execute_rollout_websocket import (
    _Credential,
    _RecoveryResponse,
    _Socket,
    _frame,
    _policy,
    _sent,
    _session,
)

_JOB = "ftjob-0123456789abcdef0123456789abcdef"
_PROTOCOL = "rle.execute-rollout.v3"
_RESULT_PREFIX = (
    "/rle/v1.0/subscriptions/11111111-1111-1111-1111-111111111111"
    "/resourceGroups/group/providers/Microsoft.MachineLearningServices/workspaces/workspace"
    "/rl_environments/rollouts/"
)


class _FineTuneCredential(_Credential):
    async def get_token(self, *args, **kwargs):
        token = await super().get_token(*args, **kwargs)
        return AccessToken("finetune-" + token.token, token.expires_on)


class _QueuedSocket(_Socket):
    subprotocol = _PROTOCOL


def _queued_session(monkeypatch, sockets, **options):
    return _session(monkeypatch, sockets, job_id=_JOB, protocol_version="v3", **options)


def _call(session, **options):
    return asyncio.create_task(session.execute_rollout(task={"private": "task-secret"}, policy=_policy(), **options))


def _result(rollout_id):
    return {
        "rollout_id": rollout_id,
        "reward": 1.0,
        "rollout": {"sequences": [{"input_ids": [1, 2], "loss_mask": [0, 1], "logprobs": [0.0, -0.5]}]},
        "result": {"answer": "sanitized"},
    }


def _terminal(rollout_id, sequence=1, **options):
    return {"sequence": sequence, "attempt_id": "attempt-2", "result": _result(rollout_id), **options}


@asynccontextmanager
async def _running(monkeypatch, protocols=(_PROTOCOL,), job_id=_JOB, **options):
    commands = asyncio.Queue()
    requests = []

    async def handle(socket):
        requests.append(socket.request)
        async for message in socket:
            header, raw = deserialize_rollout_frame(message, DEFAULT_MAX_RESPONSE_BYTES)
            commands.put_nowait((socket, header, json.loads(raw)))

    original = transport.websocket_connect
    async with serve(handle, "127.0.0.1", 0, subprotocols=list(protocols)) as server:
        port = server.sockets[0].getsockname()[1]

        async def local(url, **kwargs):
            parsed = urlsplit(url)
            assert parsed.scheme == "wss"
            assert parsed.netloc == "account.test"
            assert parsed.path.endswith("/openenv/ws")
            return await original(f"ws://127.0.0.1:{port}{parsed.path}?{parsed.query}", **kwargs)

        monkeypatch.setattr(transport, "websocket_connect", local)
        user, finetune = _Credential(), _FineTuneCredential()
        client = AIProjectClient(
            endpoint="https://account.test/api/projects/project", credential=user, allow_preview=True
        )
        async with client:
            async with client.rle.get_rollout_client(
                "environment", "1", job_id, protocol_version="v3", finetune_credential=finetune, **options
            ) as session:
                yield SimpleNamespace(
                    session=session,
                    commands=commands,
                    requests=requests,
                    user=user,
                    finetune=finetune,
                )


async def _command(run, kind="execute"):
    while True:
        item = await asyncio.wait_for(run.commands.get(), 5)
        if item[1]["type"] == kind:
            return item
        assert item[1]["type"] == "detach"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_id",
    [None, "ftjob-", "ftjob-a", "ftjob-" + "1" * 24, _JOB, "ftjob-Recipe_Run-42.a~Z", "ftjob-" + "a" * 985],
)
async def test_public_factory_preserves_opaque_job_id_through_execute_attach_and_detach(monkeypatch, job_id):
    async with _running(monkeypatch, job_id=job_id) as run:
        call = _call(run.session)
        socket, header, body = await _command(run)
        rollout_id = header["rollout_id"]
        assert header.get("job_id") == body.get("job_id") == job_id
        assert socket.subprotocol == _PROTOCOL
        await socket.send(
            _frame("progress", rollout_id, {"sequence": 0, "attempt_id": "attempt-1", "progress": {"phase": "running"}})
        )
        await socket.close()
        replacement, recovered, cursor = await _command(run, "attach")
        assert recovered.get("job_id") == job_id
        assert recovered["rollout_id"] == rollout_id
        assert cursor == {"last_sequence": 0}
        assert replacement is not socket
        await replacement.send(_frame("completed", rollout_id, _terminal(rollout_id)))
        assert (await call).reward == 1
        _, detached, _ = await _command(run, "detach")
        assert detached["rollout_id"] == rollout_id
        assert detached.get("job_id") == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_id",
    [
        "",
        "job-abc",
        "FTJOB-abc",
        " ftjob-abc",
        "ftjob-abc ",
        "ftjob-abc\n",
        "ftjob-a\rb",
        "ftjob-a\tb",
        "ftjob-a\x00b",
        "ftjob-a/b",
        "ftjob-a\\b",
        "ftjob-a?b",
        "ftjob-a#b",
        "ftjob-a%b",
        "ftjob-a:b",
        "ftjob-\u00e9",
        "ftjob-\uff11",
        "ftjob-" + "a" * 986,
        123,
        True,
        b"ftjob-abc",
    ],
)
async def test_public_factory_rejects_unsafe_job_ids_before_credentials_or_connection(monkeypatch, job_id):
    credential = _Credential()

    async def connect(*args, **kwargs):
        pytest.fail("Invalid job_id must fail before connecting.")

    monkeypatch.setattr(transport, "websocket_connect", connect)
    async with AIProjectClient("https://account.test/api/projects/project", credential, allow_preview=True) as client:
        with pytest.raises(ValueError, match="Queued job_id must start"):
            client.rle.get_rollout_client("environment", "1", job_id, protocol_version="v3")
        assert credential.calls == 0


@pytest.mark.asyncio
async def test_real_v3_handshake_multiplexing_admission_and_typed_capture(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    async with _running(monkeypatch) as run:
        assert run.user.calls == run.finetune.calls == 0
        calls = [_call(run.session, harness=HarnessConfiguration(type="gym")) for _ in range(64)]
        for _ in range(2):
            batch = [await _command(run) for _ in range(32)]
            await asyncio.sleep(0.02)
            assert run.commands.empty()
            assert len(run.session._queued._pending) == 32
            assert len(run.requests) == 1
            for socket, header, body in reversed(batch):
                rollout_id = header["rollout_id"]
                assert header["job_id"] == body["job_id"] == _JOB
                assert body["rollout_id"] == rollout_id
                assert body["harness"] == {"type": "gym"}
                assert body["policy"]["type"] == "loom"
                await socket.send(
                    _frame(
                        "accepted",
                        rollout_id,
                        {
                            "phase": "queued",
                            "attempt_id": None,
                            "last_sequence": None,
                        },
                    )
                )
                await socket.send(
                    _frame(
                        "progress",
                        rollout_id,
                        {
                            "sequence": 0,
                            "attempt_id": "attempt-1",
                            "progress": {"phase": "running"},
                        },
                    )
                )
                await socket.send(_frame("completed", rollout_id, _terminal(rollout_id)))
        results = await asyncio.gather(*calls)
        assert all(isinstance(item, RolloutResult) for item in results)
        assert all(item.rollout["sequences"][0]["logprobs"] == [0.0, -0.5] for item in results)
        request = run.requests[0]
        assert request.headers["Sec-WebSocket-Protocol"] == _PROTOCOL
        assert request.headers["Authorization"] == "Bearer token-1"
        assert request.headers["aml-user-token"] == "token-1"
        assert request.headers["x-ms-finetune-identity"] == "finetune-token-1"
        assert "x-ms-rle-user-token" not in request.headers
        assert run.user.calls == run.finetune.calls == 1
    # Server debug logs belong to the fake server; isolate the SDK's own records.
    sdk_logs = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("azure.ai.projects")
    )
    assert "token-1" not in sdk_logs
    assert "task-secret" not in sdk_logs
    assert "input_ids" not in sdk_logs


@pytest.mark.asyncio
@pytest.mark.parametrize("receive_zero", [False, True])
async def test_real_reconnect_uses_received_cursor_and_permits_gaps_and_terminal_replay(monkeypatch, receive_zero):
    async with _running(monkeypatch) as run:
        progress = []
        call = _call(run.session, progress_callback=progress.append)
        socket, header, _ = await _command(run)
        rollout_id = header["rollout_id"]
        await socket.send(
            _frame(
                "accepted",
                rollout_id,
                {
                    "phase": "queued",
                    "attempt_id": None,
                    "last_sequence": None,
                },
            )
        )
        if receive_zero:
            await socket.send(
                _frame(
                    "progress",
                    rollout_id,
                    {
                        "sequence": 0,
                        "attempt_id": "attempt-1",
                        "progress": {"phase": "running"},
                    },
                )
            )
        await socket.send(
            _frame(
                "state",
                rollout_id,
                {
                    "phase": "running",
                    "last_sequence": 90,
                    "latest_sequence": 100,
                },
            )
        )
        await socket.close()
        replacement, recovered, payload = await _command(run, "attach")
        assert recovered["rollout_id"] == rollout_id
        assert payload == {"last_sequence": 0 if receive_zero else None}
        assert run.user.calls == run.finetune.calls == 2
        await replacement.send(
            _frame(
                "progress",
                rollout_id,
                {
                    "sequence": 7,
                    "attempt_id": "attempt-2",
                    "progress": {"phase": "cleanup"},
                },
            )
        )
        await replacement.send(_frame("completed", rollout_id, _terminal(rollout_id, sequence=7)))
        assert (await call).rollout_id == rollout_id
        assert progress[-1]["sequence"] == 7
        assert progress[-1]["attempt_id"] == "attempt-2"


@pytest.mark.asyncio
async def test_real_unnegotiated_protocol_never_submits_or_falls_back(monkeypatch):
    async with _running(monkeypatch, protocols=()) as run:
        with pytest.raises(RLERolloutError) as caught:
            await run.session.execute_rollout(task={}, policy=_policy())
        assert caught.value.code == "QueuedProtocolRequired"
        assert run.commands.empty()
        assert len(run.requests) == 1


@pytest.mark.asyncio
async def test_ambiguous_send_attaches_without_second_execute(monkeypatch):
    first, second = _QueuedSocket(), _QueuedSocket()
    first.fail_send = True
    session, _, _ = _queued_session(monkeypatch, [first, second])
    async with session:
        call = _call(session)
        header, _ = await _sent(first)
        recovered, raw = await asyncio.wait_for(second.sent.get(), 3)
        assert recovered["type"] == "attach"
        assert recovered["rollout_id"] == header["rollout_id"]
        assert json.loads(raw) == {"last_sequence": None}
        second.incoming.put_nowait(_frame("completed", header["rollout_id"], _terminal(header["rollout_id"])))
        await call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,code,sequence",
    [
        ("error", "QueuedAcceptanceDisabled", None),
        ("error", "HistoryUnavailable", None),
        ("error", "DeliveryUnavailable", None),
        ("error", "RolloutNotFound", None),
        ("failed", "queued_validation_failed", None),
        ("failed", "queued_delegation_failed", None),
        ("failed", "queued_execution_failed", 3),
        ("failed", "queued_budget_expired", 3),
        ("failed", "queued_attempt_timeout", 3),
        ("error", "secret-service-code", None),
    ],
)
async def test_failures_are_explicit_sanitized_and_never_resubmitted(monkeypatch, kind, code, sequence):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        payload = {"code": code, "message": "secret-user-token", "retryable": False}
        if kind == "failed":
            payload.update(sequence=sequence, attempt_id=None if sequence is None else "attempt")
        socket.incoming.put_nowait(_frame(kind, header["rollout_id"], payload))
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == (code if code != "secret-service-code" else "QueuedRolloutFailed")
        assert "secret" not in str(caught.value)
        assert caught.value.response is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("sequence", [None, 3])
async def test_structured_grader_failure_preserves_caller_reason_through_attachment(
    monkeypatch, reconnect, sequence, caplog
):
    caplog.set_level(logging.DEBUG)
    async with _running(monkeypatch) as run:
        call = _call(run.session)
        socket, header, _ = await _command(run)
        rollout_id = header["rollout_id"]
        if reconnect:
            await socket.close()
            socket, attached, _ = await _command(run, "attach")
            assert attached["rollout_id"] == rollout_id
        cause = {
            "code": "GraderFailed",
            "dependency": "grader",
            "operation": "grading",
            "retryable": False,
            "error_type": "KeyError",
        }
        message = "The grader raised an exception: KeyError: missing score"
        await socket.send(
            _frame(
                "failed",
                rollout_id,
                {
                    "code": "queued_execution_failed",
                    "message": message,
                    "retryable": False,
                    "sequence": sequence,
                    "attempt_id": "attempt-2",
                    "cause": cause,
                },
            )
        )

        with pytest.raises(RLERolloutError) as caught:
            await call

        error = caught.value
        assert error.code == "queued_execution_failed"
        assert error.service_message == message
        assert error.cause == cause
        assert error.rollout_id == rollout_id
        assert error.attempt_id == "attempt-2"
        assert error.sequence == sequence
        assert message in str(error)
        assert message not in caplog.text
        _, detached, _ = await _command(run, "detach")
        assert detached["rollout_id"] == rollout_id
        assert run.commands.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    [
        {"code": "FutureGraderFailure", "dependency": "grader", "operation": "grading", "retryable": True},
        {
            "code": "GraderFailed",
            "dependency": "grader",
            "operation": "startup",
            "retryable": False,
            "traceback": "not a public cause field",
            "error_type": None,
        },
    ],
)
async def test_failure_cause_is_forward_compatible_but_does_not_expose_extra_fields(monkeypatch, cause):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        socket.incoming.put_nowait(
            _frame(
                "failed",
                header["rollout_id"],
                {
                    "code": "queued_execution_failed",
                    "message": "Safe service reason.",
                    "retryable": False,
                    "sequence": 1,
                    "attempt_id": "attempt",
                    "cause": cause,
                },
            )
        )
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.cause["code"] == cause["code"]
        assert "traceback" not in caught.value.cause
        assert "not a public" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause,message",
    [
        ({}, "reason"),
        ({"code": "GraderFailed", "dependency": "grader", "operation": "grading", "retryable": 1}, "reason"),
        ({"code": "unsafe\ncode", "dependency": "grader", "operation": "grading", "retryable": False}, "reason"),
        (
            {
                "code": "GraderFailed",
                "dependency": "grader",
                "operation": "grading",
                "retryable": False,
                "error_type": ["KeyError"],
            },
            "reason",
        ),
        ({"code": "GraderFailed", "dependency": "grader", "operation": "grading", "retryable": False}, "x" * 4097),
    ],
)
async def test_malformed_structured_failure_is_a_protocol_error(monkeypatch, cause, message):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        socket.incoming.put_nowait(
            _frame(
                "failed",
                header["rollout_id"],
                {
                    "code": "queued_execution_failed",
                    "message": message,
                    "retryable": False,
                    "sequence": 1,
                    "attempt_id": "attempt",
                    "cause": cause,
                },
            )
        )
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "InvalidRolloutFrame"
        assert caught.value.cause is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        "text-frame",
        b"\x00",
        b"\x00\x00\x10\x01{}",
        b'\x00\x00\x00\x24{"type":"progress","type":"completed"}{}',
        "unknown",
        "bad-json",
        "nonfinite",
        "bad-id",
        "bad-sequence",
        "missing-attempt",
        "conflict",
    ],
)
async def test_malformed_frames_raise_typed_errors(monkeypatch, malformed):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        if isinstance(malformed, bytes) or malformed == "text-frame":
            message = malformed
        elif malformed == "unknown":
            message = _frame("mystery", rollout_id)
        elif malformed == "bad-id":
            message = _frame("progress", "bad-id")
        elif malformed == "bad-json":
            message = _frame("progress", rollout_id)[:-1]
        elif malformed == "nonfinite":
            message = _frame("progress", rollout_id, {"sequence": float("nan")})
        elif malformed == "conflict":
            message = _frame("completed", rollout_id, {"rollout_id": "0" * 32})
        else:
            message = _frame(
                "progress",
                rollout_id,
                {
                    "sequence": -1 if malformed == "bad-sequence" else 0,
                    "attempt_id": None,
                    "progress": {},
                },
            )
        socket.incoming.put_nowait(message)
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "InvalidRolloutFrame"


def _result_pipeline(session, reply):
    requests, responses = [], []

    async def run(request, **kwargs):
        assert kwargs == {"stream": True, "retry_total": 0, "permit_redirects": False}
        requests.append(request)
        response = _RecoveryResponse(request, *reply(request))
        responses.append(response)
        return SimpleNamespace(http_response=response)

    session._operations._pipeline_client = SimpleNamespace(
        format_url=lambda path, **kwargs: session._operations._config.endpoint + path,
        _pipeline=SimpleNamespace(run=run),
    )
    return requests, responses


@pytest.mark.asyncio
async def test_result_reference_uses_original_project_scope_fresh_auth_and_full_typed_result(monkeypatch):
    socket = _QueuedSocket()
    finetune = _FineTuneCredential()
    session, user, _ = _queued_session(monkeypatch, [socket], finetune_credential=finetune)
    requests, responses = _result_pipeline(session, lambda request: (200, _result(request.url.split("/")[-2])))
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        socket.incoming.put_nowait(
            _frame(
                "completed",
                rollout_id,
                {
                    "sequence": 5,
                    "attempt_id": "attempt",
                    "result_url": _RESULT_PREFIX + rollout_id + "/result",
                },
            )
        )
        result = await call
        assert result.rollout_id == rollout_id
        assert result.rollout["sequences"][0]["input_ids"] == [1, 2]
        assert len(requests) == 1 and responses[0].closed
        request = requests[0]
        assert request.url.startswith(
            "https://account.test/api/projects/project/rl_environments/rollouts/" + rollout_id + "/result"
        )
        assert request.headers["Authorization"] == "Bearer token-2"
        assert request.headers["aml-user-token"] == "token-2"
        assert request.headers["x-ms-finetune-identity"] == "finetune-token-2"
        assert user.calls == finetune.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        "https://evil.test/result",
        "//evil.test/result",
        "/rl_environments/rollouts/{id}/result",
        _RESULT_PREFIX + "{id}/result?secret=sas",
        _RESULT_PREFIX + "{id}/result#fragment",
        _RESULT_PREFIX + "{other}/result",
        _RESULT_PREFIX.replace("/group/", "/%2f/") + "{id}/result",
    ],
)
async def test_invalid_result_references_never_forward_credentials(monkeypatch, route):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    requests, _ = _result_pipeline(session, lambda _: (200, {}))
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        socket.incoming.put_nowait(
            _frame(
                "completed",
                rollout_id,
                {
                    "sequence": 1,
                    "attempt_id": "attempt",
                    "result_url": route.format(id=rollout_id, other="f" * 32),
                },
            )
        )
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "InvalidRolloutResultReference"
        assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (302, {}, "RolloutResultUnavailable"),
        (401, {}, "RolloutResultUnavailable"),
        (404, {}, "RolloutResultUnavailable"),
        (200, b"x" * 2048, "RolloutResultTooLarge"),
        (200, {}, "InvalidRolloutResult"),
        (200, b"not-json", "RolloutResultUnavailable"),
    ],
)
async def test_result_lookup_status_size_and_shape_failures(monkeypatch, status, body, code):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    requests, responses = _result_pipeline(session, lambda _: (status, body))
    async with session:
        call = _call(session, max_response_bytes=1024)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        socket.incoming.put_nowait(
            _frame(
                "completed",
                rollout_id,
                {
                    "sequence": 1,
                    "attempt_id": "attempt",
                    "result_url": _RESULT_PREFIX + rollout_id + "/result",
                },
            )
        )
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == code
        assert len(requests) == 1 and responses[0].closed


@pytest.mark.asyncio
async def test_receive_continues_while_serialized_sender_is_blocked(monkeypatch):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    original = socket.send
    release = asyncio.Event()
    blocked = asyncio.Event()

    async def send(frame):
        await original(frame)
        if socket.sent.qsize() == 1:
            blocked.set()
            await release.wait()

    async with session:
        first = _call(session)
        header, _ = await _sent(socket)
        socket.send = send
        second = _call(session)
        await asyncio.wait_for(blocked.wait(), 1)
        socket.incoming.put_nowait(_frame("completed", header["rollout_id"], _terminal(header["rollout_id"])))
        assert (await asyncio.wait_for(first, 1)).reward == 1.0
        second_header, _ = await _sent(socket)
        release.set()
        socket.incoming.put_nowait(
            _frame("completed", second_header["rollout_id"], _terminal(second_header["rollout_id"]))
        )
        await second


@pytest.mark.asyncio
async def test_expiry_during_send_rotates_before_next_submission(monkeypatch):
    first, replacement = _QueuedSocket(), _QueuedSocket()
    session, credential, _ = _queued_session(monkeypatch, [first, replacement])
    original_send = first.send
    blocked = asyncio.Queue()
    release = asyncio.Semaphore(0)

    async def delayed_send(frame):
        await original_send(frame)
        blocked.put_nowait(None)
        await release.acquire()

    first.send = delayed_send
    async with session:
        warmup = _call(session, rollout_id="1" * 32)
        await asyncio.wait_for(blocked.get(), 1)
        second = _call(session, rollout_id="2" * 32)
        third = _call(session, rollout_id="3" * 32)
        client = session._queued

        async def admitted():
            while len(client._pending) != 3:
                await asyncio.sleep(0)

        await asyncio.wait_for(admitted(), 1)
        deadlines = {key: value.deadline for key, value in client._pending.items()}
        permits = session._admission._value
        release.release()
        await asyncio.wait_for(blocked.get(), 1)
        assert client._pending["2" * 32].submitted
        assert not client._pending["3" * 32].submitted
        client._expires = 0
        release.release()

        commands = [await _sent(replacement) for _ in range(3)]
        assert first.closed
        assert credential.calls == 2
        assert first.sent.qsize() == 2
        assert [(header["type"], header["rollout_id"]) for header, _ in commands] == [
            ("attach", "1" * 32),
            ("attach", "2" * 32),
            ("execute", "3" * 32),
        ]
        assert [json.loads(raw) for _, raw in commands[:2]] == [
            {"last_sequence": None},
            {"last_sequence": None},
        ]
        assert {key: value.deadline for key, value in client._pending.items()} == deadlines
        assert session._admission._value == permits
        for header, _ in commands:
            rollout_id = header["rollout_id"]
            replacement.incoming.put_nowait(_frame("completed", rollout_id, _terminal(rollout_id)))
        assert all(result.reward == 1 for result in await asyncio.gather(warmup, second, third))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "timeout", "close"])
async def test_submitted_cancellation_deadline_and_close_never_cancel_remote(monkeypatch, action):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session, timeout=0.1 if action == "timeout" else 5)
        header, _ = await _sent(socket)
        if action == "cancel":
            call.cancel()
        elif action == "close":
            await session.close()
        with pytest.raises(RLERolloutUnknownOutcomeError) as caught:
            await call
        assert caught.value.rollout_id == header["rollout_id"]
        await asyncio.sleep(0)
        while not socket.sent.empty():
            command, _ = await _sent(socket)
            assert command["type"] == "detach"


@pytest.mark.asyncio
async def test_recovery_is_bounded_and_original_deadline_not_reset(monkeypatch):
    monkeypatch.setattr(queued, "_MAX_RECONNECTS", 0)
    socket = _QueuedSocket()
    socket.fail_send = True
    session, _, calls = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session)
        await _sent(socket)
        with pytest.raises(RLERolloutUnknownOutcomeError):
            await call
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [0, 60, -1])
async def test_both_credential_lifetimes_bound_queued_handshake(monkeypatch, invalid):
    socket = _QueuedSocket()
    ft = _FineTuneCredential()
    ft.lifetime = invalid
    session, _, calls = _queued_session(monkeypatch, [socket], finetune_credential=ft)
    async with session:
        with pytest.raises(RLERolloutError) as caught:
            await session.execute_rollout(task={}, policy=_policy())
        assert caught.value.code == "RolloutCredentialFailed"
        assert not calls


@pytest.mark.asyncio
async def test_credential_failure_is_sanitized(monkeypatch):
    session, user, calls = _queued_session(monkeypatch, [_QueuedSocket()])

    async def fail(*args):
        raise ClientAuthenticationError("secret-token")

    user.get_token = fail
    async with session:
        with pytest.raises(RLERolloutError) as caught:
            await session.execute_rollout(task={}, policy=_policy())
        assert caught.value.code == "RolloutCredentialFailed"
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is None and not calls


@pytest.mark.asyncio
async def test_legacy_finetune_identity_matches_b7_and_bounds_connection_age(monkeypatch):
    ft = _FineTuneCredential()
    ft.lifetime = 120
    socket = _Socket()
    session, _, calls = _session(monkeypatch, [socket], finetune_credential=ft)
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        assert calls[0][1]["subprotocols"] == ("rle.execute-rollout.v2", "rle.execute-rollout.v1")
        assert calls[0][1]["additional_headers"]["x-ms-finetune-identity"] == "finetune-token-1"
        assert 0 < session._active.expires - time.monotonic() <= 60
        socket.incoming.put_nowait(_frame("completed", header["rollout_id"], _result(header["rollout_id"])))
        await call


@pytest.mark.parametrize(
    "options",
    [
        {"protocol_version": "v2"},
        {"protocol_version": "v4"},
        {"protocol_version": "v3", "server_managed_token_renewal": True},
        {"finetune_credential": _FineTuneCredential(), "server_managed_token_renewal": True},
        {"protocol_version": "v3", "job_id": "ftjob-unsafe/path"},
    ],
)
def test_invalid_factory_options_fail_locally(monkeypatch, options):
    job = options.pop("job_id", None)
    with pytest.raises(ValueError):
        _session(monkeypatch, [], job_id=job, **options)
    assert not hasattr(RLEOperations, "get_rollout_client")


def test_finetune_identity_header_is_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    _WEBSOCKET_LOGGER.debug("> %s: %s", "x-ms-finetune-identity", "secret-token")
    assert "secret-token" not in caplog.text
    assert "REDACTED" in caplog.text


@pytest.mark.asyncio
async def test_real_v2_only_server_is_rejected_without_fallback(monkeypatch):
    async with _running(monkeypatch, protocols=("rle.execute-rollout.v2",)) as run:
        with pytest.raises(RLERolloutError) as caught:
            await run.session.execute_rollout(task={}, policy=_policy())
        assert caught.value.code == "RolloutConnectionFailed"
        assert run.commands.empty()
        assert run.user.calls == run.finetune.calls == 1


@pytest.mark.asyncio
async def test_admission_wait_does_not_consume_original_call_deadline(monkeypatch):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket], limit=1)
    async with session:
        first = _call(session)
        first_header, _ = await _sent(socket)
        second = _call(session, timeout=0.1)
        await asyncio.sleep(0.15)
        assert not second.done()
        assert socket.sent.empty()
        socket.incoming.put_nowait(
            _frame("completed", first_header["rollout_id"], _terminal(first_header["rollout_id"]))
        )
        await first
        while True:
            second_header, _ = await _sent(socket)
            if second_header["type"] == "execute":
                break
        socket.incoming.put_nowait(
            _frame("completed", second_header["rollout_id"], _terminal(second_header["rollout_id"]))
        )
        await second


@pytest.mark.asyncio
async def test_close_wakes_admission_waiters_and_cancellation_before_send_is_not_submission(monkeypatch):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket], limit=1)
    first = _call(session)
    await _sent(socket)
    cancelled = _call(session)
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    waiter = _call(session)
    await asyncio.sleep(0)
    await session.close()
    with pytest.raises(RLERolloutUnknownOutcomeError):
        await first
    with pytest.raises(RLERolloutError) as caught:
        await waiter
    assert caught.value.code == "RolloutSessionClosed"
    assert socket.sent.empty()


@pytest.mark.asyncio
async def test_accepted_is_not_terminal_and_rotation_retains_id_deadline_and_slot(monkeypatch):
    first, replacement = _QueuedSocket(), _QueuedSocket()
    ft = _FineTuneCredential()
    ft.lifetime = 120
    session, _, _ = _queued_session(monkeypatch, [first, replacement], finetune_credential=ft)
    async with session:
        call = _call(session)
        header, _ = await _sent(first)
        rollout_id = header["rollout_id"]
        first.incoming.put_nowait(
            _frame(
                "accepted",
                rollout_id,
                {
                    "phase": "queued",
                    "attempt_id": None,
                    "last_sequence": None,
                },
            )
        )
        await asyncio.sleep(0.01)
        assert not call.done()
        client = session._queued
        pending = client._pending[rollout_id]
        original_deadline = pending.deadline
        assert 0 < client._expires - time.monotonic() <= 60
        permits = session._admission._value
        client._expires = 0
        client._wake.set()
        recovered, raw = await _sent(replacement)
        assert recovered["type"] == "attach" and recovered["rollout_id"] == rollout_id
        assert json.loads(raw) == {"last_sequence": None}
        assert pending.deadline == original_deadline
        assert session._admission._value == permits
        replacement.incoming.put_nowait(
            _frame(
                "state",
                rollout_id,
                {
                    "phase": "finalizing",
                    "last_sequence": None,
                    "latest_sequence": 5,
                },
            )
        )
        replacement.incoming.put_nowait(_frame("completed", rollout_id, _terminal(rollout_id)))
        await call


@pytest.mark.asyncio
async def test_reconnect_does_not_extend_submitted_deadline(monkeypatch):
    first = _QueuedSocket()
    session, _, calls = _queued_session(monkeypatch, [first, _QueuedSocket()])
    async with session:
        start = time.monotonic()
        call = _call(session, timeout=0.1)
        header, _ = await _sent(first)
        first.incoming.put_nowait(EOFError("socket gone"))
        with pytest.raises(RLERolloutUnknownOutcomeError) as caught:
            await call
        assert caught.value.rollout_id == header["rollout_id"]
        assert time.monotonic() - start < 0.75
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_oversized_progress_and_callback_failures_do_not_stop_siblings(monkeypatch):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])

    def fail(_):
        raise RuntimeError("secret callback data")

    async with session:
        failing = _call(session, progress_callback=fail)
        first, _ = await _sent(socket)
        oversized = _call(session, max_response_bytes=32)
        second, _ = await _sent(socket)
        valid = _call(session)
        third, _ = await _sent(socket)
        for header in (first, second):
            socket.incoming.put_nowait(
                _frame(
                    "progress",
                    header["rollout_id"],
                    {
                        "sequence": 0,
                        "attempt_id": "attempt",
                        "progress": {"phase": "running"},
                    },
                )
            )
        with pytest.raises(RLERolloutError) as caught:
            await failing
        assert caught.value.code == "RolloutProgressCallbackFailed"
        assert "secret" not in str(caught.value)
        with pytest.raises(RLERolloutError) as caught:
            await oversized
        assert caught.value.code == "RolloutResponseTooLarge"
        socket.incoming.put_nowait(_frame("completed", third["rollout_id"], _terminal(third["rollout_id"])))
        await valid


@pytest.mark.asyncio
async def test_http_pipeline_auth_failure_is_sanitized(monkeypatch):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    _result_pipeline(session, lambda _: (200, {}))

    async def fail(*args, **kwargs):
        raise ClientAuthenticationError("secret-user-token")

    session._operations._pipeline_client._pipeline.run = fail
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        socket.incoming.put_nowait(
            _frame(
                "completed",
                rollout_id,
                {
                    "sequence": 1,
                    "attempt_id": "attempt",
                    "result_url": _RESULT_PREFIX + rollout_id + "/result",
                },
            )
        )
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "RolloutResultUnavailable"
        assert "secret" not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_actual_azure_redirect_policy_never_forwards_result_auth(monkeypatch):
    from azure.core.pipeline import AsyncPipeline
    from azure.core.pipeline.policies import AsyncRedirectPolicy

    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    requests, responses = _result_pipeline(session, lambda _: (302, {}))

    class Transport:
        async def send(self, request, **kwargs):
            requests.append(request)
            response = _RecoveryResponse(request, 302, {})
            response.headers["Location"] = "https://evil.test/result"
            responses.append(response)
            return response

    session._operations._pipeline_client._pipeline = AsyncPipeline(
        transport=Transport(), policies=[AsyncRedirectPolicy()]
    )
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        socket.incoming.put_nowait(
            _frame(
                "completed",
                rollout_id,
                {
                    "sequence": 1,
                    "attempt_id": "attempt",
                    "result_url": _RESULT_PREFIX + rollout_id + "/result",
                },
            )
        )
        with pytest.raises(RLERolloutError) as caught:
            await call
        assert caught.value.code == "RolloutResultUnavailable"
        assert len(requests) == 1 and responses[0].closed
        assert requests[0].url.startswith("https://account.test/api/projects/project/")


@pytest.mark.asyncio
async def test_already_dispatched_completion_wins_cancellation_race(monkeypatch):
    socket = _QueuedSocket()
    session, _, _ = _queued_session(monkeypatch, [socket])
    async with session:
        call = _call(session)
        header, _ = await _sent(socket)
        rollout_id = header["rollout_id"]
        session._queued._pending[rollout_id].future.set_result(_terminal(rollout_id))
        call.cancel()
        assert (await call).rollout_id == rollout_id
