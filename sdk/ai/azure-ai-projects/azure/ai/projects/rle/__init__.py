# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Types for Reinforcement Learning Environments (RLE).

Everything the :attr:`~azure.ai.projects.AIProjectClient.rle` operations accept or return lives
here, without the ``RLE`` prefix the flat ``azure.ai.projects.models`` namespace needs.

Helpers for authoring environments are in :mod:`azure.ai.projects.rle.environments`, which needs
the optional ``rle`` dependency group and is not imported here.
"""

from ..models._enums import (
    RLEInstanceStatus as InstanceStatus,
    RLEnvironmentDiskImageConversionStatus as EnvironmentDiskImageConversionStatus,
    RLEnvironmentVersionBump as EnvironmentVersionBump,
    RLEPaginationOrder as PaginationOrder,
)
from ..models._models import (
    CreateRLEInstanceGroupRequest as CreateInstanceGroupRequest,
    CreateRLEnvironmentRequest as CreateEnvironmentRequest,
    ListRLEInstanceGroupsResponse as ListInstanceGroupsResponse,
    ListRLEnvironmentsResponse as ListEnvironmentsResponse,
    ListRLEnvironmentVersionsResponse as ListEnvironmentVersionsResponse,
    RLEInstance as Instance,
    RLEInstanceGroup as InstanceGroup,
    RLEInstanceGroupResourceProfile as InstanceGroupResourceProfile,
    RLEnvironment as Environment,
    RLEnvironmentState as EnvironmentState,
    RLEResetRequest as ResetRequest,
    RLEStepRequest as StepRequest,
    RLEStepResult as StepResult,
)
from ._rollout_models import (
    FoundryDeploymentPolicy,
    HarnessConfiguration,
    LoomPolicy,
    RolloutEpisode,
    RolloutGrade,
    RolloutPolicy,
    RolloutRequest,
    RolloutResult,
    RolloutStep,
    SamplingOptions,
)

__all__ = [
    "CreateEnvironmentRequest",
    "CreateInstanceGroupRequest",
    "Environment",
    "EnvironmentDiskImageConversionStatus",
    "EnvironmentState",
    "EnvironmentVersionBump",
    "FoundryDeploymentPolicy",
    "HarnessConfiguration",
    "Instance",
    "InstanceGroup",
    "InstanceGroupResourceProfile",
    "InstanceStatus",
    "ListEnvironmentVersionsResponse",
    "ListEnvironmentsResponse",
    "ListInstanceGroupsResponse",
    "LoomPolicy",
    "PaginationOrder",
    "ResetRequest",
    "RolloutEpisode",
    "RolloutGrade",
    "RolloutPolicy",
    "RolloutRequest",
    "RolloutResult",
    "RolloutStep",
    "SamplingOptions",
    "StepRequest",
    "StepResult",
]
