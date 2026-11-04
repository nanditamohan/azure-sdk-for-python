# pylint: disable=line-too-long,useless-suppression
# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Models for the RLE Execute Rollout operation.

Execute Rollout runs one complete episode of a published RLE environment version and returns
what it produced. The service owns the loop -- it resets the environment, asks the model for a
completion, applies the action, and repeats until the environment terminates or the version's
step budget is spent. The caller supplies only the task and the model binding.

That is the difference from the instance-level reset/step operations, where the caller owns the
loop and drives an instance it leased. Here nothing is leased and nothing is stepped by hand; a
rollout is one request and one result.

The result carries both halves of what a training run needs: ``reward`` (and ``success`` where a
grader produced a verdict) and ``rollout``, the Capture Proxy graph, whose ``sequences`` hold the
``input_ids``/``loss_mask``/``logprobs`` of every model turn the episode sampled. When an environment
graded the rollout, ``grade`` also says how: its status and reward, and which grading contract it used.
"""

from typing import Any, List, Literal, Mapping, Optional, overload

from .._utils.model_base import Model as _Model, rest_discriminator, rest_field

__all__ = [
    "RolloutPolicy",
    "LoomPolicy",
    "FoundryDeploymentPolicy",
    "SamplingOptions",
    "HarnessConfiguration",
    "RolloutRequest",
    "RolloutResult",
    "RolloutEpisode",
    "RolloutStep",
    "RolloutGrade",
]

_VISIBILITY = ["read", "create", "update", "delete", "query"]


class RolloutPolicy(_Model):
    """Where a rollout's weights come from, discriminated by ``type``.

    A rollout has to say which backend resolves its weights before it can say anything about
    them, because the fields that identify weights mean different things to different backends.
    ``type`` is that statement; the concrete subclass carries whatever its backend requires.

    :ivar type: Discriminator naming the backend. Required.
    :vartype type: str
    """

    __mapping__: dict[str, _Model] = {}
    type: str = rest_discriminator(name="type", visibility=_VISIBILITY)
    """Discriminator naming the backend that resolves this policy's weights. Required."""


class LoomPolicy(RolloutPolicy, discriminator="loom"):
    """Weights held in a Loom training session, pinned to one immutable checkpoint.

    A rollout never samples from a deployment by name alone. ``session_id`` and ``checkpoint_id``
    pin it to exact weights, which is what makes the returned trajectory on-policy for that
    checkpoint and therefore trainable. The two are supplied together; the service rejects one
    without the other.

    ``project_endpoint`` is supplied per rollout rather than taken from the client, because one
    RLE deployment serves callers from many projects and the caller's token is presented against
    this endpoint.

    :ivar type: Always ``"loom"``. Required.
    :vartype type: str
    :ivar model_name: Training model to sample from. Required.
    :vartype model_name: str
    :ivar project_endpoint: Foundry project endpoint whose Loom sampler serves this rollout. Required.
    :vartype project_endpoint: str
    :ivar session_id: Loom training session holding the weights. Required, with ``checkpoint_id``.
    :vartype session_id: str
    :ivar checkpoint_id: Immutable Loom checkpoint identifier. Required, with ``session_id``.
    :vartype checkpoint_id: str
    :ivar sequence_id: Optional Loom sequence identifier for this rollout.
    :vartype sequence_id: int
    """

    type: Literal["loom"] = rest_discriminator(name="type", visibility=_VISIBILITY)  # type: ignore
    """Always ``\"loom\"``. Required."""
    model_name: str = rest_field(visibility=_VISIBILITY)
    """Training model to sample from. Required."""
    project_endpoint: str = rest_field(visibility=_VISIBILITY)
    """Foundry project endpoint whose Loom sampler serves this rollout. Required."""
    session_id: str = rest_field(visibility=_VISIBILITY)
    """Loom training session holding the weights. Required, with ``checkpoint_id``."""
    checkpoint_id: str = rest_field(visibility=_VISIBILITY)
    """Immutable Loom checkpoint identifier. Required, with ``session_id``."""
    sequence_id: Optional[int] = rest_field(visibility=_VISIBILITY)
    """Optional Loom sequence identifier for this rollout."""

    @overload
    def __init__(
        self,
        *,
        model_name: str,
        project_endpoint: str,
        session_id: str,
        checkpoint_id: str,
        sequence_id: Optional[int] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, type="loom", **kwargs)


class FoundryDeploymentPolicy(RolloutPolicy, discriminator="foundry"):
    """A model deployment in the caller's Foundry project, sampled by name.

    Unlike :class:`LoomPolicy` there are no weights to pin, so there is no session, checkpoint
    or sequence identifier, and the service rejects them. Capture is text-level and eval-only: the
    rollout carries no token ids or logprobs and is never trainable, so :attr:`RolloutResult.sequences`
    is empty. Use :class:`LoomPolicy` for training.

    The deployment must be in the project named by ``project_endpoint``; the caller's own token is
    used to reach it, so no API key is supplied.

    :ivar type: Always ``"foundry"``. Required.
    :vartype type: str
    :ivar deployment_name: Name of the model deployment in the project. Required.
    :vartype deployment_name: str
    :ivar project_endpoint: Foundry project endpoint that hosts the deployment. Required.
    :vartype project_endpoint: str
    """

    type: Literal["foundry"] = rest_discriminator(name="type", visibility=_VISIBILITY)  # type: ignore
    """Always ``\"foundry\"``. Required."""
    deployment_name: str = rest_field(visibility=_VISIBILITY)
    """Name of the model deployment in the project. Required."""
    project_endpoint: str = rest_field(visibility=_VISIBILITY)
    """Foundry project endpoint that hosts the deployment. Required."""

    @overload
    def __init__(self, *, deployment_name: str, project_endpoint: str) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, type="foundry", **kwargs)


class SamplingOptions(_Model):
    """How a rollout's completions are sampled and rendered.

    Separate from :class:`RolloutPolicy` on purpose: a renderer describes how a completion is
    turned into tokens, not where the weights came from, so it applies to every policy kind that
    captures a trainable graph rather than being a property of one of them. A
    :class:`FoundryDeploymentPolicy` has no renderer, and the service rejects ``renderer_name`` for it.

    :ivar renderer_name: Optional Capture Proxy renderer profile. The service picks a compatible
     default when omitted.
    :vartype renderer_name: str
    """

    renderer_name: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Optional Capture Proxy renderer profile."""

    @overload
    def __init__(self, *, renderer_name: Optional[str] = None) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class HarnessConfiguration(_Model):
    """The callable target that acts on the task for one rollout.

    ``type`` selects the target. ``gym`` (the service default) drives the environment's own
    Gym/OpenEnv loop. ``byoh`` calls an agent you host at ``endpoint``. ``package`` runs a named
    harness package and takes ``name``. ``name`` is valid only with ``package``; ``endpoint`` and
    ``protocol`` only with ``byoh``.

    For ``protocol="responses"`` the ``endpoint`` is the Foundry agent URL
    ``{project_endpoint}/agents/{agent_name}``; the service appends the Responses protocol path.

    :ivar type: ``gym``, ``byoh`` or ``package``. Required.
    :vartype type: str
    :ivar name: Harness package name. Only with ``package``.
    :vartype name: str
    :ivar endpoint: Agent URL. Only with ``byoh``.
    :vartype endpoint: str
    :ivar protocol: ``responses`` or ``rle-invocation``. Only with ``byoh``.
    :vartype protocol: str
    """

    type: str = rest_field(visibility=_VISIBILITY)
    """``gym``, ``byoh`` or ``package``. Required."""
    name: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Harness package name. Only with ``package``."""
    endpoint: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Agent URL. Only with ``byoh``."""
    protocol: Optional[str] = rest_field(visibility=_VISIBILITY)
    """``responses`` or ``rle-invocation``. Only with ``byoh``."""

    @overload
    def __init__(
        self,
        *,
        type: str,
        name: Optional[str] = None,
        endpoint: Optional[str] = None,
        protocol: Optional[str] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class RolloutRequest(_Model):
    """Request body for one Execute Rollout call.

    ``task`` is one opaque JSON value -- normally a complete dataset row -- forwarded unchanged to
    the environment's ``reset``. The service does not inspect or reinterpret it, so its schema is
    the environment's own contract, not RLE's.

    :ivar rollout_id: Caller-generated identifier, reserved within the project before any resource
     is allocated and returned unchanged. Required.
    :vartype rollout_id: str
    :ivar task: Opaque task record passed verbatim to the environment's reset. Required.
    :vartype task: any
    :ivar policy: Where this rollout's weights come from. Required.
    :vartype policy: ~azure.ai.projects.rle.RolloutPolicy
    :ivar job_id: Optional training-job identifier used for durable rollout correlation.
    :vartype job_id: str
    :ivar sampling: How this rollout's completions are sampled and rendered. Optional.
    :vartype sampling: ~azure.ai.projects.rle.SamplingOptions
    :ivar harness: Optional callable target for this rollout only. The environment version's
     stored harness is used when omitted.
    :vartype harness: ~azure.ai.projects.rle.HarnessConfiguration
    """

    rollout_id: str = rest_field(visibility=_VISIBILITY)
    """Caller-generated identifier, returned unchanged. Required."""
    task: Any = rest_field(visibility=_VISIBILITY)
    """Opaque task record passed verbatim to the environment's reset. Required."""
    policy: RolloutPolicy = rest_field(visibility=_VISIBILITY)
    """Where this rollout's weights come from. Required."""
    job_id: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Training-job identifier used for durable rollout correlation."""
    sampling: Optional[SamplingOptions] = rest_field(visibility=_VISIBILITY)
    """How this rollout's completions are sampled and rendered."""
    harness: Optional[HarnessConfiguration] = rest_field(visibility=_VISIBILITY)
    """Optional callable target for this rollout only; the version's stored harness when omitted."""

    @overload
    def __init__(
        self,
        *,
        rollout_id: str,
        task: Any,
        policy: RolloutPolicy,
        job_id: Optional[str] = None,
        sampling: Optional[SamplingOptions] = None,
        harness: Optional[HarnessConfiguration] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class RolloutStep(_Model):
    """One environment step of a Gym/OpenEnv episode, bound to the model turn that produced it.

    :ivar capture_node_id: Capture Proxy model-turn identifier that produced this action.
    :vartype capture_node_id: str
    :ivar reward: Environment reward returned for this action.
    :vartype reward: float
    :ivar episode_done: Whether the environment ended the episode on this step.
    :vartype episode_done: bool
    """

    capture_node_id: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Capture Proxy model-turn identifier that produced this action."""
    reward: Optional[float] = rest_field(visibility=_VISIBILITY)
    """Environment reward returned for this action."""
    episode_done: Optional[bool] = rest_field(visibility=_VISIBILITY)
    """Whether the environment ended the episode on this step."""

    @overload
    def __init__(
        self,
        *,
        capture_node_id: Optional[str] = None,
        reward: Optional[float] = None,
        episode_done: Optional[bool] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class RolloutEpisode(_Model):
    """Gym/OpenEnv episode annotations. Absent for Harness and BYOH targets, which have no episode.

    :ivar kind: Discriminator for the episode shape.
    :vartype kind: str
    :ivar termination_reason: Why the episode stopped.
    :vartype termination_reason: str
    :ivar steps: Ordered environment steps.
    :vartype steps: list[~azure.ai.projects.rle.RolloutStep]
    :ivar ungraded: Set when the episode ended without the environment ever reporting termination,
     so its reward is a running sum rather than a grade. Omitted when the environment did grade it,
     which keeps a genuine zero distinguishable from an episode that was never scored.
    :vartype ungraded: bool
    """

    kind: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Discriminator for the episode shape."""
    termination_reason: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Why the episode stopped."""
    steps: Optional[List[RolloutStep]] = rest_field(visibility=_VISIBILITY)
    """Ordered environment steps."""
    ungraded: Optional[bool] = rest_field(visibility=_VISIBILITY)
    """Set when the episode ended without the environment ever reporting termination."""

    @overload
    def __init__(
        self,
        *,
        kind: Optional[str] = None,
        termination_reason: Optional[str] = None,
        steps: Optional[List[RolloutStep]] = None,
        ungraded: Optional[bool] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class RolloutGrade(_Model):
    """How the environment graded a rollout.

    ``status`` is always ``"graded"`` today, because a grading failure is returned as an error and
    never as a reward of 0. ``"failed"`` is reserved so that a failed grade can later be reported
    here, with a ``None`` reward and an ``error`` saying why.

    ``contract`` names the grading contract the reward came from. It is present only when the
    environment graded with the context RLE builds from the captured model calls (see
    :class:`~azure.ai.projects.rle.environments.GradeContext`). It is absent when RLE used the legacy
    fallback for an environment that cannot take that context, which grades the final answer alone
    before the model calls are captured.

    :ivar status: ``"graded"`` when the environment produced ``reward``, or ``"failed"`` when grading
     failed. Required.
    :vartype status: str
    :ivar reward: The reward the environment graded the rollout with. ``None`` when ``status`` is
     ``"failed"``.
    :vartype reward: float
    :ivar contract: Version of the grading context contract the environment graded with. Absent when
     RLE used the legacy fallback.
    :vartype contract: int
    :ivar error: Why grading failed. Present only when ``status`` is ``"failed"``.
    :vartype error: str
    """

    status: str = rest_field(visibility=_VISIBILITY)
    """``graded`` when the environment produced a reward, or ``failed`` when grading failed. Required."""
    reward: Optional[float] = rest_field(visibility=_VISIBILITY)
    """The reward the environment graded the rollout with. ``None`` when grading failed."""
    contract: Optional[int] = rest_field(visibility=_VISIBILITY)
    """Version of the grading context contract the environment graded with. Absent for the legacy fallback."""
    error: Optional[str] = rest_field(visibility=_VISIBILITY)
    """Why grading failed. Present only when ``status`` is ``failed``."""

    @overload
    def __init__(
        self,
        *,
        status: str,
        reward: Optional[float] = None,
        contract: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class RolloutResult(_Model):
    """What one completed rollout produced.

    ``success`` is a grader's verdict and is therefore absent for Gym/OpenEnv, which has no
    grader: an environment reports reward, and inferring a boolean from it would state a result
    the environment never gave. Read ``reward`` for those, and ``success`` only where a Harness
    grader supplied it.

    ``grade`` is present when the service reports how the rollout was graded. ``reward``,
    ``success`` and ``result`` keep their meaning whether or not it is.

    :ivar rollout_id: The identifier supplied on the request, returned unchanged.
    :vartype rollout_id: str
    :ivar rollout: Capture Proxy graph for the episode, with credentials and routing metadata
     removed. Its ``sequences`` carry the ``input_ids``, ``loss_mask`` and ``logprobs`` of every
     model turn.
    :vartype rollout: dict[str, any]
    :ivar reward: Reward for the rollout. For Gym/OpenEnv this is the undiscounted sum of every
     per-step reward in ``episode``, each of which is also reported individually.
    :vartype reward: float
    :ivar success: Task-level verdict from a Harness grader. Absent for Gym/OpenEnv.
    :vartype success: bool
    :ivar result: Optional structured target result, with protected runtime values removed.
    :vartype result: any
    :ivar episode: Gym/OpenEnv episode annotations. Absent for Harness and BYOH.
    :vartype episode: ~azure.ai.projects.rle.RolloutEpisode
    :ivar grade: How the environment graded the rollout. Absent when the service did not report it.
    :vartype grade: ~azure.ai.projects.rle.RolloutGrade
    """

    rollout_id: str = rest_field(visibility=_VISIBILITY)
    """The identifier supplied on the request, returned unchanged."""
    rollout: Optional[Any] = rest_field(visibility=_VISIBILITY)
    """Capture Proxy graph for the episode."""
    reward: Optional[float] = rest_field(visibility=_VISIBILITY)
    """Reward for the rollout."""
    success: Optional[bool] = rest_field(visibility=_VISIBILITY)
    """Task-level verdict from a Harness grader. Absent for Gym/OpenEnv."""
    result: Optional[Any] = rest_field(visibility=_VISIBILITY)
    """Optional structured target result."""
    episode: Optional[RolloutEpisode] = rest_field(visibility=_VISIBILITY)
    """Gym/OpenEnv episode annotations."""
    grade: Optional[RolloutGrade] = rest_field(visibility=_VISIBILITY)
    """How the environment graded the rollout."""

    @overload
    def __init__(
        self,
        *,
        rollout_id: str,
        rollout: Optional[Any] = None,
        reward: Optional[float] = None,
        success: Optional[bool] = None,
        result: Optional[Any] = None,
        episode: Optional[RolloutEpisode] = None,
        grade: Optional[RolloutGrade] = None,
    ) -> None: ...

    @overload
    def __init__(self, mapping: Mapping[str, Any]) -> None:
        """
        :param mapping: raw JSON to initialize the model.
        :type mapping: Mapping[str, Any]
        """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    @property
    def sequences(self) -> List[Any]:
        """The graph's trainable sequences, one per root-to-leaf path, or ``[]`` if it has none.

        Each carries ``input_ids``, ``loss_mask`` and ``logprobs`` for the turns along that path.
        An eval-level capture returns no sequences, so this is empty rather than absent -- callers
        building training data should check it before assuming the rollout is trainable.
        """
        rollout = self.rollout
        if not isinstance(rollout, Mapping):
            return []
        sequences = rollout.get("sequences")
        return list(sequences) if isinstance(sequences, list) else []
