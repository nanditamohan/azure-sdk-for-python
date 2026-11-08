# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------

import copy
import pickle
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, Optional

import pytest

pytest.importorskip("openenv")
pytest.importorskip("fastmcp")

from fastmcp import FastMCP
from openenv.core.env_server.mcp_environment import MCPEnvironment  # type: ignore[import-untyped]
from openenv.core.env_server.mcp_types import (  # type: ignore[import-untyped]
    CallToolAction,
    ListToolsAction,
)
from openenv.core.env_server.types import Action, Observation, State  # type: ignore[import-untyped]

from azure.ai.projects.rle.environments import GradeAction, GradeContext, RLEnvironment


def _context() -> Dict[str, Any]:
    """A fresh context as RLE sends it, so tests can mutate it freely."""
    return {
        "version": 1,
        "id": "rollout-1",
        "trace": {
            "schema_version": "ATIF-v1.8",
            "session_id": "rollout-1",
            "agent": {"name": "byoh", "version": "1"},
            "steps": [{"step_id": 1, "source": "agent", "llm_call_count": 1}],
            "final_metrics": {"total_steps": 1},
            "extra": {},
        },
        "output_format": "custom",
        "output": {"text": "42", "citations": ["a"]},
        "termination_reason": "answered",
        "task": {"question": "6 * 7", "answer": "42"},
        "rewards": {"reward": 1.0},
        "env_report": {},
    }


class _TestEnvironment(RLEnvironment):
    def reset(
        self,
        seed: Optional[int] = None,
        episode_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Observation:
        self._set_state(State(episode_id=episode_id))
        return Observation(metadata={"seed": seed, **kwargs})

    def grade(
        self,
        action: GradeAction,
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Observation:
        current_state = self.state
        self._set_state(
            current_state.model_copy(
                update={"step_count": current_state.step_count + 1}
            )
        )
        return Observation(
            reward=1.0 if action.response == "correct" else 0.0,
            done=True,
            metadata={"timeout_s": timeout_s, **kwargs},
        )


def test_public_environment_inherits_openenv_mcp_environment() -> None:
    assert issubclass(RLEnvironment, MCPEnvironment)


def test_subclass_must_implement_reset_and_grade() -> None:
    assert RLEnvironment.__abstractmethods__ == frozenset({"grade", "reset"})

    class MissingImplementations(RLEnvironment):
        pass

    with pytest.raises(TypeError, match="abstract"):
        MissingImplementations()


def test_grade_action_requires_string_response() -> None:
    with pytest.raises(ValueError):
        GradeAction()  # type: ignore[call-arg]

    with pytest.raises(ValueError):
        GradeAction(response=1)  # type: ignore[arg-type]


def test_grade_action_rollout_graph_defaults_to_none() -> None:
    assert GradeAction(response="correct").rollout_graph is None


def test_grade_action_accepts_rollout_graph() -> None:
    rollout_graph = {"turns": [{"tool_call_errors": [{"error_code": "invalid_json"}]}]}

    action = GradeAction(response="correct", rollout_graph=rollout_graph)

    assert action.rollout_graph == rollout_graph


def test_grade_action_context_defaults_to_none() -> None:
    assert GradeAction(response="correct").context is None


def test_grade_action_accepts_context() -> None:
    action = GradeAction(response="42", context=_context())

    assert action.context == _context()


def test_grade_action_validates_with_and_without_context() -> None:
    # OpenEnv builds the action with ``model_validate`` on the step body it receives.
    legacy = GradeAction.model_validate({"response": "42"})
    current = GradeAction.model_validate({"response": "42", "context": _context()})

    assert legacy.context is None
    assert current.context == _context()
    assert legacy.response == current.response == "42"


def test_grade_action_still_requires_response_with_context() -> None:
    with pytest.raises(ValueError):
        GradeAction(context=_context())  # type: ignore[call-arg]


@pytest.mark.parametrize("context", ["text", 1, ["a"]])
def test_grade_action_context_must_be_an_object(context: Any) -> None:
    with pytest.raises(ValueError):
        GradeAction(response="42", context=context)


def test_grade_action_schema_declares_context() -> None:
    # RLE decides which grading contract to use from the environment's ``GET /schema``,
    # which OpenEnv serves as ``action_cls.model_json_schema()``. It sends ``context``
    # only when this schema declares the property. Removing or renaming the field would
    # silently downgrade every environment to the legacy contract.
    schema = GradeAction.model_json_schema()

    assert "context" in schema["properties"]
    assert "rollout_graph" in schema["properties"]
    # Only ``response`` may be required, so a legacy step without context stays valid.
    assert schema["required"] == ["response"]


def test_action_class_without_context_forbids_it() -> None:
    # This is why RLE negotiates by schema instead of always sending ``context``:
    # OpenEnv actions forbid unknown fields, so a class that predates ``context``
    # rejects a step that carries it.
    class LegacyGradeAction(Action):
        response: str

    assert "context" not in LegacyGradeAction.model_json_schema()["properties"]
    assert LegacyGradeAction.model_validate({"response": "42"}).response == "42"
    with pytest.raises(ValueError):
        LegacyGradeAction.model_validate({"response": "42", "context": {}})


def test_grade_context_is_not_part_of_the_wire_shape() -> None:
    action = GradeAction(response="42", context=_context())

    assert "grade_context" not in action.model_dump()
    assert "grade_context" not in GradeAction.model_json_schema()["properties"]


def test_grade_context_wraps_a_real_context() -> None:
    action = GradeAction(response="42", context=_context())

    context = action.grade_context

    assert isinstance(context, GradeContext)
    assert context.version == 1
    assert context.id == "rollout-1"
    assert context.output_format == "custom"
    assert context.output == {"text": "42", "citations": ["a"]}
    assert context.termination_reason == "answered"
    assert context.task == {"question": "6 * 7", "answer": "42"}
    assert context.rewards == {"reward": 1.0}
    assert context.env_report == {}
    assert context.trace["schema_version"] == "ATIF-v1.8"
    assert dict(context) == _context()


@pytest.mark.parametrize(
    ("output_format", "output"),
    [
        ("custom", {"text": "42", "citations": ["a"]}),
        ("custom", [{"answer": 42}]),
        (
            "responses",
            [
                {"type": "reasoning", "summary": []},
                {"type": "function_call", "name": "lookup", "arguments": "{}"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "42"}],
                },
            ],
        ),
    ],
)
def test_grade_context_preserves_structured_output(
    output_format: str, output: Any
) -> None:
    action = GradeAction(
        response="not the structured answer",
        context={**_context(), "output_format": output_format, "output": output},
    )

    context = action.grade_context

    assert context.output_format == output_format
    assert action.context is not None
    assert context.output is action.context["output"]
    assert context.output == output
    with pytest.raises(AttributeError):
        _ = context.response_text
    with pytest.raises(KeyError):
        _ = context["outputs"]


def test_grade_context_adapts_an_earlier_text_context() -> None:
    context = GradeAction(
        response="not the context answer",
        context={
            "version": 1,
            "response_text": "42",
            "outputs": {"citations": ["a"]},
        },
    ).grade_context

    assert context.version == 1
    assert context.output_format == "custom"
    assert context.output == {"text": "42"}
    assert context["response_text"] == "42"
    assert context["outputs"] == {"citations": ["a"]}


def test_earlier_text_does_not_override_explicit_structured_output() -> None:
    context = GradeContext({**_context(), "response_text": "obsolete"})

    assert context.output == _context()["output"]


def test_grade_context_is_synthesized_for_a_legacy_action() -> None:
    action = GradeAction(response="42")

    context = action.grade_context

    assert isinstance(context, GradeContext)
    assert context.version == 0
    assert context.output_format == "custom"
    assert context.output == {"text": "42"}
    assert dict(context) == {
        "version": 0,
        "id": "",
        "trace": {},
        "output_format": "custom",
        "output": {"text": "42"},
        "termination_reason": "",
        "task": {},
        "rewards": {},
        "env_report": {},
    }


def test_legacy_grade_contexts_do_not_share_nested_containers() -> None:
    first = GradeAction(response="42").grade_context
    first.task["leaked"] = True

    assert GradeAction(response="42").grade_context.task == {}
    assert isinstance(first.output, dict)
    first.output["text"] = "changed"
    assert GradeAction(response="42").grade_context.output == {"text": "42"}


def test_grade_context_follows_the_current_context() -> None:
    action = GradeAction(response="42")
    assert action.grade_context.version == 0

    action.context = _context()

    assert action.grade_context.version == 1


class _ContextEnvironment(_TestEnvironment):
    """Grades from ``grade_context`` only, the way an environment author would."""

    def grade(
        self,
        action: GradeAction,
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Observation:
        context = action.grade_context
        answered = context.termination_reason in ("", "answered")
        output = context.output
        assert isinstance(output, dict)
        return Observation(
            reward=1.0 if answered and output["text"] == "42" else 0.0,
            done=True,
            metadata={"version": context.version},
        )


def test_one_grade_implementation_handles_current_and_legacy_actions() -> None:
    environment = _ContextEnvironment()

    legacy = environment.step(GradeAction(response="42"))
    current = environment.step(GradeAction(response="42", context=_context()))
    truncated = environment.step(
        GradeAction(
            response="42",
            context={**_context(), "termination_reason": "max_steps"},
        )
    )

    assert (legacy.reward, legacy.metadata) == (1.0, {"version": 0})
    assert (current.reward, current.metadata) == (1.0, {"version": 1})
    assert (truncated.reward, truncated.metadata) == (0.0, {"version": 1})


def test_grade_context_is_exported_from_the_environments_package() -> None:
    import azure.ai.projects.rle.environments as environments

    assert "GradeContext" in environments.__all__
    assert environments.GradeContext is GradeContext


def test_grade_context_reads_by_key_and_by_attribute() -> None:
    context = GradeContext(_context())

    assert context["output"] is context.output
    assert context["output_format"] == context.output_format == "custom"
    assert context["task"] is context.task
    assert context["version"] == context.version == 1
    assert context["termination_reason"] == context.termination_reason
    assert context["id"] == context.id == "rollout-1"


def test_grade_context_is_a_mapping() -> None:
    context = GradeContext(_context())

    assert isinstance(context, Mapping)
    assert list(context) == list(_context())
    assert len(context) == 9
    assert "trace" in context
    assert "missing" not in context
    assert context.get("missing") is None
    assert context.get("version") == 1
    assert context == _context()
    assert context != {"version": 1}


def test_grade_context_cannot_be_mutated() -> None:
    context = GradeContext(_context())

    with pytest.raises(TypeError):
        context["version"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        del context["version"]  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="read only"):
        context.version = 2  # type: ignore[misc]
    with pytest.raises(AttributeError, match="read only"):
        context.added = 1
    with pytest.raises(AttributeError, match="read only"):
        del context.version
    for method in ("update", "pop", "popitem", "clear", "setdefault"):
        assert not hasattr(context, method)

    assert dict(context) == _context()


def test_grade_context_unknown_keys_stay_readable() -> None:
    context = GradeContext({**_context(), "future_field": {"a": [1, 2]}})

    assert context["future_field"] == {"a": [1, 2]}
    assert context.future_field == {"a": [1, 2]}
    assert "future_field" in context
    assert len(context) == 10


def test_grade_context_unknown_attribute_raises_attribute_error() -> None:
    context = GradeContext(_context())

    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = context.nope
    with pytest.raises(KeyError):
        _ = context["nope"]
    assert not hasattr(context, "nope")
    assert getattr(context, "nope", "fallback") == "fallback"


def test_grade_context_underscore_names_are_keys_only() -> None:
    context = GradeContext({"_private": 1})

    assert context["_private"] == 1
    with pytest.raises(AttributeError):
        _ = context._private


def test_grade_context_key_named_like_a_mapping_method_is_read_by_key() -> None:
    context = GradeContext({"items": [1]})

    assert context["items"] == [1]
    assert callable(context.items)


def test_grade_context_nested_values_stay_plain_json() -> None:
    context = GradeContext(_context())

    assert type(context.trace) is dict
    assert type(context.trace["steps"]) is list
    assert type(context.task) is dict
    assert type(context.rewards) is dict


def test_grade_context_fills_omitted_and_null_fields_with_empty_values() -> None:
    context = GradeContext(
        {"version": 1, "task": None, "output_format": None, "output": None}
    )

    assert context.version == 1
    assert context.id == ""
    assert context.trace == {}
    assert context.output_format == "custom"
    assert context.output == {}
    assert context.termination_reason == ""
    assert context.task == {}
    assert context.rewards == {}
    assert context.env_report == {}
    assert len(context) == 9


@pytest.mark.parametrize("fields", [{}, {"output": None}])
def test_grade_context_empty_responses_output_stays_an_array(
    fields: Dict[str, Any]
) -> None:
    context = GradeContext({"output_format": "responses", **fields})

    assert context.output == []
    assert context.output is not GradeContext({"output_format": "responses"}).output


@pytest.mark.parametrize(
    ("output_format", "output", "reward", "error"),
    [
        ("custom", {"text": "shipped"}, 1.0, None),
        ("custom", {"text": "pending"}, 0.0, None),
        ("custom", {"text": 42}, None, "string text"),
        ("custom", [], None, "custom text"),
        ("responses", [], None, "custom text"),
    ],
)
def test_readme_grader_uses_the_real_context(
    output_format: str, output: Any, reward: Optional[float], error: Optional[str]
) -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    example = (
        readme.split("This example expects the built-in custom text convention:", 1)[1]
        .split("```python\n", 1)[1]
        .split("```", 1)[0]
    )
    namespace = {"GradeAction": GradeAction, "Observation": Observation, "Any": Any}
    exec(dedent(example), namespace)
    action = GradeAction(
        response="not the structured answer",
        context={
            **_context(),
            "output_format": output_format,
            "output": output,
            "trace": {"steps": [{"tool_calls": [{"function_name": "lookup_order"}]}]},
        },
    )

    if error:
        with pytest.raises(ValueError, match=error):
            namespace["grade"](None, action)
    else:
        assert namespace["grade"](None, action).reward == reward


def test_grade_context_keeps_null_unknown_keys() -> None:
    assert GradeContext({"future_field": None})["future_field"] is None


def test_grade_context_requires_a_mapping() -> None:
    with pytest.raises(TypeError, match="requires a mapping"):
        GradeContext(["version"])  # type: ignore[arg-type]


def test_grade_context_survives_copy_and_pickle() -> None:
    context = GradeContext(_context())

    for clone in (
        copy.copy(context),
        copy.deepcopy(context),
        pickle.loads(pickle.dumps(context)),
    ):
        assert isinstance(clone, GradeContext)
        assert clone == context
    assert copy.deepcopy(context).trace is not context.trace


def test_grade_action_dispatches_to_grade() -> None:
    environment = _TestEnvironment()

    observation = environment.step(
        GradeAction(response="correct"), timeout_s=5.0, attempt=1
    )

    assert observation.reward == 1.0
    assert observation.done is True
    assert observation.metadata == {"timeout_s": 5.0, "attempt": 1}
    assert environment.state.step_count == 1


def test_non_mcp_action_must_be_grade_action() -> None:
    environment = _TestEnvironment()

    with pytest.raises(TypeError, match="Expected GradeAction"):
        environment.step(Action())


def test_environment_can_have_no_custom_mcp_tools() -> None:
    environment = _TestEnvironment()

    observation = environment.step(ListToolsAction())

    assert observation.tools == []


def test_tool_decorator_registers_callable_mcp_tool() -> None:
    environment = _TestEnvironment()

    @environment.tool()
    def echo(value: str) -> str:
        return value

    listed = environment.step(ListToolsAction())
    called = environment.step(
        CallToolAction(tool_name="echo", arguments={"value": "hello"})
    )

    assert [tool.name for tool in listed.tools] == ["echo"]
    assert called.error is None
    assert "hello" in str(called.result)


def test_preconfigured_mcp_server_preserves_registered_tools() -> None:
    server = FastMCP("preconfigured")

    @server.tool()
    def lookup(value: str) -> str:
        return value

    environment = _TestEnvironment(server)

    observation = environment.step(ListToolsAction())

    assert [tool.name for tool in observation.tools] == ["lookup"]


def test_state_returns_defensive_snapshot() -> None:
    environment = _TestEnvironment()
    environment.reset(episode_id="episode-1")

    snapshot = environment.state
    snapshot.step_count = 10

    assert environment.state.episode_id == "episode-1"
    assert environment.state.step_count == 0


def test_set_state_rejects_non_openenv_state() -> None:
    environment = _TestEnvironment()

    with pytest.raises(TypeError, match="OpenEnv State"):
        environment._set_state(object())  # type: ignore[arg-type]
