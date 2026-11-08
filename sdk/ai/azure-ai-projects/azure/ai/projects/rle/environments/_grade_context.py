# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""The read-only grading context RLE sends with a rollout's final answer."""

from collections.abc import Iterator, Mapping
from typing import Any, Dict, List, NoReturn, Tuple, Union

__all__ = ["GradeContext"]


def _empty_context() -> Dict[str, Any]:
    # Fresh containers on every call: the nested dicts are plain and mutable, so they must not be shared.
    return {
        "version": 0,
        "id": "",
        "trace": {},
        "output_format": "custom",
        "output": {},
        "termination_reason": "",
        "task": {},
        "rewards": {},
        "env_report": {},
    }


class GradeContext(Mapping[str, Any]):
    """Everything RLE knows about one finished rollout, as one read-only mapping.

    RLE builds the context after the harness finishes and the model calls are captured, and sends it
    with the final answer in :attr:`~azure.ai.projects.rle.environments.GradeAction.context`. Read a
    field by key or as an attribute: ``context.output`` and ``context["output"]`` are the
    same value. Everything below the top level is a plain ``dict``, ``list`` or JSON scalar.

    Every field listed below is always present, and a field with no source is empty (``""``, ``{}`` or ``[]``),
    so a grader never tests for a missing key. Within a ``version`` RLE only adds fields, and a breaking
    change bumps ``version``. Keys this class does not know about stay readable by key and as
    attributes, and a consumer must tolerate them. A key that collides with a :class:`~collections.abc.Mapping`
    method, such as ``items``, is readable by key only.

    The context cannot be changed: item and attribute assignment and deletion raise. The nested values
    are the ones RLE sent and are not copied, so treat them as read only too.

    Older contexts with ``response_text`` but neither output field are adapted to
    ``output_format="custom"`` and ``output={"text": response_text}``. Their original keys stay
    readable. No legacy answer alias is synthesized for structured output.

    :param data: The raw ``context`` object. Fields it omits, or sends as ``null``, read as empty,
        except ``output_format``, which defaults to ``"custom"``. Missing or null ``output`` is
        ``[]`` for ``"responses"`` and ``{}`` otherwise.
    :type data: Mapping[str, Any]
    """

    __slots__ = ("_data",)
    _data: Dict[str, Any]

    def __init__(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise TypeError(f"{type(self).__name__} requires a mapping, got {type(data).__name__}")
        values = _empty_context()
        for key, value in data.items():
            if value is not None or key not in values:
                values[key] = value
        if "output_format" not in data and "output" not in data and isinstance(data.get("response_text"), str):
            values["output"] = {"text": data["response_text"]}
        elif values["output_format"] == "responses" and data.get("output") is None:
            values["output"] = []
        object.__setattr__(self, "_data", values)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup fails. Underscore names stay out of the key space so that
        # internals and protocol probes such as ``__deepcopy__`` never resolve to a context key.
        if not name.startswith("_"):
            try:
                return self._data[name]
            except KeyError:
                pass
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is read only")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is read only")

    def __reduce__(self) -> Tuple[type, Tuple[Dict[str, Any]]]:
        # Rebuild through the constructor, because the read-only attributes block the default state restore.
        return (type(self), (dict(self._data),))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    @property
    def version(self) -> int:
        """The contract version this context was built for.

        ``1`` for the current contract. ``0`` means RLE sent no context and the SDK synthesized this one
        from the answer alone, see :attr:`~azure.ai.projects.rle.environments.GradeAction.grade_context`.

        :return: The contract version.
        :rtype: int
        """
        return self._data["version"]

    @property
    def id(self) -> str:
        """The rollout id.

        :return: The rollout id.
        :rtype: str
        """
        return self._data["id"]

    @property
    def trace(self) -> Dict[str, Any]:
        """An ATIF v1.8 trajectory of the model calls RLE captured.

        It has ``schema_version``, ``session_id``, ``agent``, ``steps``, ``final_metrics`` and ``extra``,
        with one agent step per model call. RLE builds it from the captured calls, never from files the
        agent wrote. ``{}`` when there is no trace.

        :return: The trajectory.
        :rtype: dict[str, any]
        """
        return self._data["trace"]

    @property
    def output_format(self) -> str:
        """The format of :attr:`output`: ``"custom"`` or ``"responses"``.

        ``"custom"`` when omitted or null. Consumers must handle unknown formats explicitly.

        :return: The declared output format.
        :rtype: str
        """
        return self._data["output_format"]

    @property
    def output(self) -> Union[Dict[str, Any], List[Any]]:
        """The structured final output, preserved without flattening or copying.

        ``"custom"`` accepts an object or array. Built-in environments conventionally use
        ``{"text": "<final answer>"}``; other custom structures are task-specific.
        ``"responses"`` is the native Responses output array, including message, reasoning and
        tool-call items. Answer readers must select message content of type ``"output_text"``.

        :return: The final output object or array.
        :rtype: dict[str, any] or list[any]
        """
        return self._data["output"]

    @property
    def termination_reason(self) -> str:
        """Why the run stopped.

        The vocabulary is open: ``"answered"``, ``"truncated"``, a named limit such as ``"timeout"``,
        ``"max_steps"``, ``"max_tokens"`` or ``"context_limit"``, ``"invalid_output"``, ``"refusal"``,
        ``"unknown"``, or a producer specific ``"<ns>:<kind>"``. Treat a value you do not recognize as
        ``"unknown"``. ``""`` when RLE sent no context.

        :return: The termination reason.
        :rtype: str
        """
        return self._data["termination_reason"]

    @property
    def task(self) -> Dict[str, Any]:
        """The task payload exactly as the rollout request carried it.

        For JSONL tasksets this is the resolved row, including grading metadata, not just its name.

        :return: The task payload.
        :rtype: dict[str, any]
        """
        return self._data["task"]

    @property
    def rewards(self) -> Dict[str, float]:
        """A flat map from reward name to the finite number the environment reported.

        ``{}`` when it reported none. JSONL tasksets have no native environment reward.

        :return: The reported rewards.
        :rtype: dict[str, float]
        """
        return self._data["rewards"]

    @property
    def env_report(self) -> Dict[str, Any]:
        """The sanitized environment report, or ``{}`` when none is available.

        Oversized reports may be replaced with a truncation marker. JSONL tasksets have no native
        environment report. This is environment-provided evidence, not captured model-call evidence.

        :return: The environment report.
        :rtype: dict[str, any]
        """
        return self._data["env_report"]
