"""Tests for the RLE Execute Rollout operation.

These run the real client pipeline against a local HTTP server rather than a mocked transport,
so the URL, query string, headers and body under assertion are the bytes that would go on the
wire -- the things that a stub of our own construction would simply agree with us about.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

import pytest

from azure.ai.projects import AIProjectClient
from azure.ai.projects.aio import AIProjectClient as AsyncAIProjectClient
from azure.ai.projects.rle import (
    FoundryDeploymentPolicy,
    HarnessConfiguration,
    LoomPolicy,
    RolloutGrade,
    RolloutResult,
    SamplingOptions,
)
from azure.ai.projects.operations import RLERolloutError
from azure.ai.projects.operations._patch_rle_rollout import (
    EXECUTE_ROLLOUT_API_VERSION,
    FORWARDED_TOKEN_HEADER,
    build_rollout_body,
)
from azure.core.credentials import AccessToken

_TOKEN = "test-forwarded-token"


class _FakeCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes = scopes
        return AccessToken(_TOKEN, 9999999999)

    def close(self) -> None:
        pass


class _AsyncFakeCredential(_FakeCredential):
    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return super().get_token(*scopes, **kwargs)

    async def close(self) -> None:
        pass


class _Recorder:
    """One captured request plus the canned reply to answer it with."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status = 200
        self.body: Any = {"rollout_id": "unset"}


def _serve(recorder: _Recorder) -> "tuple[HTTPServer, str]":
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's required spelling
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            recorder.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                }
            )
            payload = json.dumps(recorder.body).encode()
            self.send_response(recorder.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}/api/projects/proj"


@pytest.fixture
def rollout_server():
    recorder = _Recorder()
    server, endpoint = _serve(recorder)
    try:
        yield recorder, endpoint
    finally:
        server.shutdown()
        server.server_close()


def _client(endpoint: str) -> AIProjectClient:
    return AIProjectClient(
        endpoint=endpoint,
        credential=_FakeCredential(),
        allow_preview=True,
    )


def _rollout(client: AIProjectClient, *args: Any, **kwargs: Any):
    """Call the operation against the loopback server.

    ``enforce_https=False`` is azure-core's per-request escape from the bearer policy's TLS
    guard, needed only because this server is plain HTTP. It travels through the operation's
    ``**kwargs``, so these tests also pin that pipeline options reach the transport.
    """
    return client.rle.execute_rollout(*args, enforce_https=False, **kwargs)


def _policy(**overrides: Any) -> LoomPolicy:
    values: Dict[str, Any] = {
        "model_name": "Qwen/Qwen3-32B",
        "project_endpoint": "https://acct.services.ai.azure.com/api/projects/proj",
        "session_id": "session-1",
        "checkpoint_id": "checkpoint-1",
    }
    values.update(overrides)
    return LoomPolicy(**values)


# --------------------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------------------


def test_posts_to_the_version_pinned_action_url(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1", "reward": 1.0}

    with _client(endpoint) as client:
        _rollout(client, "math_rl", "1.0.6", task={"seed": 1}, policy=_policy())

    path = recorder.requests[0]["path"]
    assert "/api/projects/proj/rl_environments/math_rl/versions/1.0.6:executeRollout" in path
    assert f"api-version={EXECUTE_ROLLOUT_API_VERSION}" in path


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("harness", [
    None,
    HarnessConfiguration(type="gym"),
    HarnessConfiguration(
        type="byoh", endpoint="https://account.test/api/projects/project/agents/agent", protocol="responses"
    ),
    HarnessConfiguration(type="byoh", endpoint="https://harness.example.test/run", protocol="rle-invocation"),
    HarnessConfiguration(type="package", name="opencode"),
])
async def test_http_harness_override_on_both_clients(rollout_server, asynchronous, harness):
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "fixed"}
    if asynchronous:
        async with AsyncAIProjectClient(
            endpoint=endpoint, credential=_AsyncFakeCredential(), allow_preview=True
        ) as client:
            await client.rle.execute_rollout(
                "environment", "1", task={}, policy=_policy(), harness=harness,
                rollout_id="fixed", enforce_https=False,
            )
    else:
        with _client(endpoint) as client:
            _rollout(client, "environment", "1", task={}, policy=_policy(), harness=harness, rollout_id="fixed")
    body = recorder.requests[0]["body"]
    if harness is None:
        assert "harness" not in body
    else:
        assert body["harness"] == harness.as_dict()


def test_forwards_the_caller_token_in_both_headers(rollout_server):
    """RLE needs ``aml-user-token`` in addition to ``Authorization``; it forwards that one to Loom."""
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1"}

    with _client(endpoint) as client:
        _rollout(client, "math_rl", "1.0.6", task={"seed": 1}, policy=_policy())

    headers = recorder.requests[0]["headers"]
    assert headers[FORWARDED_TOKEN_HEADER] == _TOKEN
    assert headers["authorization"] == f"Bearer {_TOKEN}"


def test_sends_task_policy_and_sampling_verbatim(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1"}
    task = {"problem": "2+2", "nested": {"rows": [1, 2, 3]}}

    with _client(endpoint) as client:
        _rollout(
            client,
            "math_rl",
            "1.0.6",
            task=task,
            policy=_policy(sequence_id=7),
            sampling=SamplingOptions(renderer_name="qwen3"),
            rollout_id="fixed-id",
        )

    body = recorder.requests[0]["body"]
    assert body["rollout_id"] == "fixed-id"
    assert body["task"] == task
    assert body["policy"] == {
        "type": "loom",
        "model_name": "Qwen/Qwen3-32B",
        "project_endpoint": "https://acct.services.ai.azure.com/api/projects/proj",
        "session_id": "session-1",
        "checkpoint_id": "checkpoint-1",
        "sequence_id": 7,
    }
    assert body["sampling"] == {"renderer_name": "qwen3"}


def test_escapes_environment_name_and_version(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1"}

    with _client(endpoint) as client:
        _rollout(client, "odd/name", "1.0.0+beta", task={}, policy=_policy())

    path = recorder.requests[0]["path"]
    assert "odd%2Fname/versions/1.0.0%2Bbeta:executeRollout" in path


def test_generates_a_bare_hex_rollout_id_when_omitted():
    body = build_rollout_body(task={}, policy=_policy())
    assert len(body.rollout_id) == 32
    assert body.rollout_id == body.rollout_id.lower()
    int(body.rollout_id, 16)


def test_rejects_a_rollout_with_no_task():
    with pytest.raises(ValueError, match="task is required"):
        build_rollout_body(task=None, policy=_policy())


@pytest.mark.parametrize(
    "name,version", [("", "1.0.0"), ("math_rl", "")], ids=["no-name", "no-version"]
)
def test_rejects_an_unpinned_environment(rollout_server, name, version):
    _, endpoint = rollout_server
    with _client(endpoint) as client:
        with pytest.raises(ValueError):
            _rollout(client, name, version, task={}, policy=_policy())


# --------------------------------------------------------------------------------------
# Response handling
# --------------------------------------------------------------------------------------


def test_returns_reward_episode_and_trainable_sequences(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {
        "rollout_id": "r1",
        "reward": 0.9,
        "rollout": {
            "sequences": [
                {"input_ids": [1, 2, 3], "loss_mask": [0, 1, 1], "logprobs": [-0.1, -0.2, -0.3]}
            ]
        },
        "episode": {
            "kind": "gym_openenv",
            "termination_reason": "environment_terminated",
            "steps": [{"capture_node_id": "n1", "reward": 0.9, "episode_done": True}],
        },
    }

    with _client(endpoint) as client:
        result = _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert result.rollout_id == "r1"
    assert result.reward == 0.9
    assert result.episode.termination_reason == "environment_terminated"
    assert result.episode.steps[0].capture_node_id == "n1"
    assert result.sequences[0]["input_ids"] == [1, 2, 3]


def test_success_stays_absent_for_gym_rather_than_defaulting_to_false(rollout_server):
    """Gym has no grader. A missing verdict must read as unknown, not as a failure."""
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1", "reward": 1.0}

    with _client(endpoint) as client:
        result = _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert result.success is None


def test_sequences_is_empty_for_an_eval_capture(rollout_server):
    """An eval rollout carries reward but no token ids, so there is nothing to train on."""
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1", "reward": 1.0, "rollout": {"sequences": []}}

    with _client(endpoint) as client:
        result = _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert result.sequences == []


def test_sequences_is_empty_when_the_graph_is_absent(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1", "reward": 1.0}

    with _client(endpoint) as client:
        result = _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert result.sequences == []


def test_returns_the_grade_the_environment_reported(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {
        "rollout_id": "r1",
        "reward": 0.75,
        "success": True,
        "grade": {"status": "graded", "reward": 0.75, "contract": 1},
    }

    with _client(endpoint) as client:
        result = _rollout(client, "byoh_env", "1.0.0", task={}, policy=_policy())

    assert isinstance(result.grade, RolloutGrade)
    assert result.grade.status == "graded"
    assert result.grade.reward == 0.75
    assert result.grade.contract == 1
    assert result.grade.error is None
    # The top level fields keep their meaning next to the grade.
    assert result.reward == 0.75
    assert result.success is True


def test_grade_has_no_contract_when_rle_used_the_legacy_fallback(rollout_server):
    """``contract`` is how a caller tells the new grading contract from the legacy fallback."""
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1", "reward": 0.5, "grade": {"status": "graded", "reward": 0.5}}

    with _client(endpoint) as client:
        result = _rollout(client, "byoh_env", "1.0.0", task={}, policy=_policy())

    assert result.grade.status == "graded"
    assert result.grade.reward == 0.5
    assert result.grade.contract is None
    assert "contract" not in result.grade.as_dict()


def test_grade_is_absent_when_the_service_does_not_report_it(rollout_server):
    recorder, endpoint = rollout_server
    recorder.body = {"rollout_id": "r1", "reward": 1.0}

    with _client(endpoint) as client:
        result = _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert result.grade is None
    assert "grade" not in result.as_dict()


def test_a_failed_grade_has_no_reward_and_says_why():
    """``failed`` is reserved for the future. It must never read as a reward of 0."""
    result = RolloutResult(
        {"rollout_id": "r1", "grade": {"status": "failed", "reward": None, "error": "grader crashed"}}
    )

    assert result.grade.status == "failed"
    assert result.grade.reward is None
    assert result.grade.error == "grader crashed"


@pytest.mark.parametrize(
    "payload",
    [
        {"rollout_id": "r1", "reward": 1.0},
        {"rollout_id": "r1", "reward": 1.0, "grade": {"status": "graded", "reward": 1.0, "contract": 1}},
        {"rollout_id": "r1", "reward": 0.5, "grade": {"status": "graded", "reward": 0.5}},
        {"rollout_id": "r1", "grade": {"status": "failed", "reward": None, "error": "grader crashed"}},
    ],
    ids=["no-grade", "new-contract", "legacy-fallback", "failed"],
)
def test_a_result_round_trips_with_and_without_a_grade(payload):
    result = RolloutResult(payload)

    assert result.as_dict() == payload
    assert RolloutResult(result.as_dict()).as_dict() == payload


def test_a_result_built_from_a_grade_object_serialises_it():
    result = RolloutResult(
        rollout_id="r1",
        reward=1.0,
        grade=RolloutGrade(status="graded", reward=1.0, contract=1),
    )

    assert result.as_dict() == {
        "rollout_id": "r1",
        "reward": 1.0,
        "grade": {"status": "graded", "reward": 1.0, "contract": 1},
    }
    assert RolloutResult(result.as_dict()).grade.contract == 1


def test_rollout_grade_is_exported_from_the_rle_namespace():
    import azure.ai.projects.rle as rle

    assert "RolloutGrade" in rle.__all__
    assert rle.RolloutGrade is RolloutGrade


# --------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------


def test_surfaces_the_service_failure_code_and_rollout_id(rollout_server):
    recorder, endpoint = rollout_server
    recorder.status = 500
    recorder.body = {
        "code": "RolloutDependencyFailed",
        "message": "The Capture Proxy dependency could not complete the rollout "
        "Gym model completion. HTTP 503",
    }

    with _client(endpoint) as client:
        with pytest.raises(RLERolloutError) as caught:
            _rollout(
                client, "math_rl", "1.0.6", task={}, policy=_policy(), rollout_id="abc123"
            )

    error = caught.value
    assert error.code == "RolloutDependencyFailed"
    assert error.rollout_id == "abc123"
    assert "HTTP 503" in str(error)
    # The rollout id belongs in the message too: it is the key the service logs the cause under.
    assert "abc123" in str(error)


def test_surfaces_a_nested_error_envelope_from_a_gateway(rollout_server):
    """A gateway ahead of RLE answers in the nested shape, not RLE's flat one."""
    recorder, endpoint = rollout_server
    recorder.status = 403
    recorder.body = {"error": {"code": "AuthorizationFailed", "message": "no data action"}}

    with _client(endpoint) as client:
        with pytest.raises(RLERolloutError) as caught:
            _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert caught.value.code == "AuthorizationFailed"
    assert "no data action" in str(caught.value)


def test_still_raises_when_the_error_body_is_not_json(rollout_server):
    """A failure to parse a failure must not replace it -- the status still has to reach the caller."""
    recorder, endpoint = rollout_server
    recorder.status = 502
    recorder.body = "<html>Bad Gateway</html>"

    with _client(endpoint) as client:
        with pytest.raises(RLERolloutError) as caught:
            _rollout(client, "math_rl", "1.0.6", task={}, policy=_policy())

    assert caught.value.status_code == 502
    assert caught.value.code is None


# --------------------------------------------------------------------------------------
# Policy discrimination
# --------------------------------------------------------------------------------------


def test_the_policy_declares_its_type_without_the_caller_saying_so():
    """``type`` is the service's discriminator, so the SDK must never leave it to the caller.

    A policy object that serialised without it would be rejected by the service for a reason the
    caller has no way to act on, having never been offered the field.
    """
    body = build_rollout_body(task={}, policy=_policy())

    assert body.policy["type"] == "loom"


def test_sampling_is_absent_rather_than_null_when_unset():
    """An omitted renderer must not become ``"renderer_name": null``.

    The service reads a present-but-blank renderer as a caller asking for a renderer it cannot
    name, and rejects it; only true absence selects the compatible default.
    """
    body = build_rollout_body(task={}, policy=_policy())

    assert "sampling" not in body.as_dict()


def test_a_policy_is_required():
    with pytest.raises(ValueError, match="policy is required"):
        build_rollout_body(task={}, policy=None)  # type: ignore[arg-type]


def test_a_serialised_policy_deserialises_back_to_its_concrete_type():
    """The discriminator is load-bearing in both directions.

    A round trip that returned the base class would make ``session_id`` unreachable on anything
    read back from the wire, which is what a union exists to prevent.
    """
    from azure.ai.projects.rle import LoomPolicy, RolloutRequest

    sent = build_rollout_body(task={"seed": 0}, policy=_policy(sequence_id=3))
    received = RolloutRequest(sent.as_dict())

    assert isinstance(received.policy, LoomPolicy)
    assert received.policy.session_id == "session-1"
    assert received.policy.sequence_id == 3


# --------------------------------------------------------------------------------------
# Foundry policy


def _foundry_policy() -> FoundryDeploymentPolicy:
    return FoundryDeploymentPolicy(
        deployment_name="gpt-5-mini",
        project_endpoint="https://acct.services.ai.azure.com/api/projects/proj",
    )


def test_a_foundry_policy_serialises_with_no_loom_fields():
    """A deployment has no weights to pin, so the service rejects session and checkpoint ids."""
    body = build_rollout_body(task={}, policy=_foundry_policy())

    assert body.as_dict()["policy"] == {
        "type": "foundry",
        "deployment_name": "gpt-5-mini",
        "project_endpoint": "https://acct.services.ai.azure.com/api/projects/proj",
    }


def test_a_serialised_foundry_policy_deserialises_back_to_its_concrete_type():
    from azure.ai.projects.rle import RolloutRequest

    sent = build_rollout_body(task={"seed": 0}, policy=_foundry_policy())
    received = RolloutRequest(sent.as_dict())

    assert isinstance(received.policy, FoundryDeploymentPolicy)
    assert received.policy.deployment_name == "gpt-5-mini"


def test_harness_is_sent_when_given():
    harness = HarnessConfiguration(
        type="byoh",
        protocol="responses",
        endpoint="https://acct.services.ai.azure.com/api/projects/proj/agents/my-agent",
    )

    body = build_rollout_body(task={}, policy=_policy(), harness=harness).as_dict()

    assert body["harness"] == {
        "type": "byoh",
        "protocol": "responses",
        "endpoint": "https://acct.services.ai.azure.com/api/projects/proj/agents/my-agent",
    }


def test_harness_is_absent_rather_than_null_when_unset():
    body = build_rollout_body(task={}, policy=_policy())

    assert "harness" not in body.as_dict()


def test_a_package_harness_carries_only_its_name():
    body = build_rollout_body(
        task={}, policy=_policy(), harness=HarnessConfiguration(type="package", name="opencode")
    ).as_dict()

    assert body["harness"] == {"type": "package", "name": "opencode"}
