# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""OpenEnv-compatible base classes for Foundry RLE environments."""

from abc import abstractmethod
from typing import Any, Dict, Optional

try:
    from fastmcp import FastMCP
    from openenv.core.env_server.mcp_environment import MCPEnvironment  # type: ignore[import-untyped]
    from openenv.core.env_server.types import Action, Observation, State  # type: ignore[import-untyped]
except ImportError as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "Foundry RLE environment authoring requires the optional dependencies. "
        'Install them with `pip install "azure-ai-projects[rle]"`.'
    ) from exc

from ._grade_context import GradeContext


class GradeAction(Action):
    """An action containing the agent's response to grade.

    RLE sends it as the terminal step of a rollout. Pass this class as the action class when creating
    the OpenEnv server. RLE reads the server's ``/schema`` action schema, and it sends the ``context``
    field only when that schema declares it, which this class does. Environments that use an older SDK
    or a custom action class never see ``context``, and RLE grades them with ``response`` alone.

    :param response: The agent's response submitted for grading.
    :type response: str
    :param rollout_graph: The sanitized rollout graph RLE captured for this episode (turns,
        tool calls, and any tool-call parse errors the model produced), when the rollout
        target makes one available. ``None`` for targets or RLE versions that do not supply
        it.
    :type rollout_graph: dict or None
    :param context: The grading context RLE builds after the harness finishes and its model calls
        are captured: the trace, the final answer, why the run stopped, the task and more. See
        :class:`~azure.ai.projects.rle.environments.GradeContext` for the fields. ``None`` when the
        service did not send one. Read :attr:`grade_context` instead of this raw object to handle
        both cases with one code path.
    :type context: dict or None
    """

    response: str
    rollout_graph: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None

    @property
    def grade_context(self) -> GradeContext:
        """The grading context for this action, with one code path for every service version.

        This wraps :attr:`context` when RLE sent one. When it sent none, because the service predates the
        contract, it is a synthesized legacy context: ``version`` is ``0``, ``response_text`` is
        :attr:`response`, and every other field is empty.

        :return: The read-only grading context.
        :rtype: ~azure.ai.projects.rle.environments.GradeContext
        """
        if self.context is None:
            return GradeContext({"version": 0, "response_text": self.response})
        return GradeContext(self.context)


class RLEnvironment(MCPEnvironment):
    """Base class for implementing a Foundry Reinforcement Learning Environment.

    Subclasses must implement :meth:`reset` to start an episode and :meth:`grade`
    to grade an agent answer. MCP tools are optional. Register tools after calling
    this constructor with OpenEnv's inherited ``tool`` decorator, or pass a
    preconfigured FastMCP server.

    :param mcp_server: An optional FastMCP server containing pre-registered tools.
        When omitted, an empty server is created and tools can be registered with
        the inherited ``tool`` decorator.
    :type mcp_server: ~fastmcp.FastMCP or None
    """

    def __init__(self, mcp_server: Optional[FastMCP] = None) -> None:
        self._state = State()
        server = mcp_server if mcp_server is not None else FastMCP("foundry-rle")
        super().__init__(server)

    @abstractmethod
    def reset(
        self,
        seed: Optional[int] = None,
        episode_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Observation:
        """Reset the environment and return the initial observation.

        :param seed: An optional seed for reproducible episodes.
        :type seed: int or None
        :param episode_id: An optional caller-provided episode identifier.
        :type episode_id: str or None
        :return: The initial episode observation.
        :rtype: ~openenv.core.env_server.types.Observation
        """

    @abstractmethod
    def grade(
        self,
        action: GradeAction,
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Observation:
        """Grade a response submitted by the agent.

        :param action: The response to grade.
        :type action: ~azure.ai.projects.rle.environments.GradeAction
        :param timeout_s: An optional grading timeout in seconds.
        :type timeout_s: float or None
        :return: The observation containing the grade and terminal state.
        :rtype: ~openenv.core.env_server.types.Observation
        """

    @property
    def state(self) -> State:
        """Return a defensive snapshot of the current environment state.

        :return: The current environment state.
        :rtype: ~openenv.core.env_server.types.State
        """

        return self._state.model_copy(deep=True)

    def _set_state(self, state: State) -> None:
        if not isinstance(state, State):
            raise TypeError("state must be an OpenEnv State")
        self._state = state.model_copy(deep=True)

    def _step_impl(
        self,
        action: Action,
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Observation:
        if not isinstance(action, GradeAction):
            raise TypeError("Expected GradeAction for a non-MCP action")
        return self.grade(action, timeout_s=timeout_s, **kwargs)
