# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Offline tests for the shared CLI model catalog and SDK rollout preflight."""

import io
import json
from types import SimpleNamespace

import pytest

from azure.ai.projects.operations import _patch_rle_rollout as validation
from azure.ai.projects.rle import FoundryDeploymentPolicy, LoomPolicy, SamplingOptions

_QWEN38_RENDERERS = (
    "qwen3_8_low_reasoning",
    "qwen3_8",
    "qwen3_8_medium_reasoning",
    "qwen3_8_disable_thinking",
)
_NEMOTRON_RENDERERS = (
    "nemotron3_ultra",
    "nemotron3_ultra_medium_thinking",
    "nemotron3_ultra_disable_thinking",
    "nemotron3_ultra_preserve_thinking",
)
_SUBMISSION_CATALOG = {
    "MAI/MAI-Code-1.1-Flash": ("mai_harmony", "mai_harmony_internal"),
    "Qwen/Qwen3.8-27B": _QWEN38_RENDERERS,
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16": _NEMOTRON_RENDERERS,
    "Qwen/Qwen3.6-35B-A3B": (
        "qwen3_5_ext",
        "qwen3_5_ext_disable_thinking",
        "qwen3_5",
        "qwen3_5_disable_thinking",
    ),
    "Qwen/Qwen3.8-27B-256K": _QWEN38_RENDERERS,
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16-256K": _NEMOTRON_RENDERERS,
}


@pytest.fixture(autouse=True)
def clear_catalog_cache(monkeypatch):
    monkeypatch.setattr(validation, "_model_catalog", None)
    monkeypatch.setattr(validation, "_model_catalog_error", None)


@pytest.fixture
def submission_catalog(monkeypatch):
    data = json.dumps(
        [
            {"model": model, "renderers": renderers}
            for model, renderers in _SUBMISSION_CATALOG.items()
        ]
    ).encode()
    monkeypatch.setattr(
        validation, "urlopen", lambda *_args, **_kwargs: io.BytesIO(data)
    )


@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize(
    "model,renderer,accepted",
    [
        (model, renderer, renderer is None or renderer in renderers)
        for model, renderers in _SUBMISSION_CATALOG.items()
        for renderer in (
            None,
            *dict.fromkeys(
                renderer
                for values in _SUBMISSION_CATALOG.values()
                for renderer in values
            ),
            "unknown-renderer",
        )
    ],
)
def test_six_model_catalog_accepts_only_matching_renderers(
    submission_catalog, mapping, model, renderer, accepted
):
    body = _body(model, renderer, mapping=mapping)
    if accepted:
        validation.validate_rollout_model_renderer(body)
    else:
        with pytest.raises(ValueError):
            validation.validate_rollout_model_renderer(body)


@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize(
    "model",
    [
        "Qwen/Qwen3-32B",
        "Qwen/Qwen3-14B",
        "Qwen/Qwen3.5-9B",
        "Qwen/Qwen3-32B-Instruct",
        "MAI/MAI-Code-1-Flash",
        "qwen/qwen3.8-27b",
        "other/Qwen3.8-27B",
        "Qwen/Qwen3.8-72B",
        "Qwen/Qwen3.8-27B:checkpoint",
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-BF16:checkpoint",
    ],
)
def test_six_model_catalog_rejects_legacy_models_and_aliases(
    submission_catalog, mapping, model
):
    with pytest.raises(ValueError):
        validation.validate_rollout_model_renderer(_body(model, mapping=mapping))


def _body(model="Qwen/Qwen3.8-27B", renderer=None, *, mapping=False):
    policy = LoomPolicy(
        model_name=model,
        project_endpoint="https://account.test/api/projects/project",
        session_id="session",
        checkpoint_id="checkpoint",
    )
    sampling = SamplingOptions(renderer_name=renderer) if renderer is not None else None
    return validation.build_rollout_body(
        task={},
        policy=policy.as_dict() if mapping else policy,
        sampling=sampling.as_dict() if mapping and sampling is not None else sampling,
    )


@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize(
    "model,renderer,accepted",
    [
        ("Qwen/Qwen3.8-27B", None, True),
        (" Qwen/Qwen3.8-27B ", " qwen3_8 ", True),
        ("Qwen/Qwen3.8-27B", "mai_harmony_internal", False),
        ("Qwen/Qwen3.8-27B", "QWEN3_8", False),
        ("qwen/qwen3.8-27b", "qwen3_8", False),
        ("Unknown/model", None, False),
        ("", "qwen3_8", False),
        ("Qwen/Qwen3.8-27B", " ", False),
    ],
)
def test_typed_and_mapping_policies_share_model_renderer_validation(
    monkeypatch, mapping, model, renderer, accepted
):
    monkeypatch.setattr(
        validation,
        "_load_model_catalog",
        lambda: {
            "Qwen/Qwen3.8-27B": ("qwen3_8", "qwen3_8_medium_reasoning"),
            "MAI/MAI-Code-1.1-Flash": ("mai_harmony", "mai_harmony_internal"),
        },
    )
    if accepted:
        validation.validate_rollout_model_renderer(
            _body(model, renderer, mapping=mapping)
        )
    else:
        with pytest.raises(ValueError):
            validation.validate_rollout_model_renderer(
                _body(model, renderer, mapping=mapping)
            )


def test_foundry_deployments_do_not_fetch_the_loom_catalog(monkeypatch):
    def forbidden():
        pytest.fail("A Foundry deployment must not load a Loom catalog")

    monkeypatch.setattr(validation, "_load_model_catalog", forbidden)
    body = validation.build_rollout_body(
        task={},
        policy=FoundryDeploymentPolicy(
            deployment_name="arbitrary-deployment",
            project_endpoint="https://account.test/api/projects/project",
        ),
    )
    validation.validate_rollout_model_renderer(body)
    body.sampling = SamplingOptions(renderer_name="qwen3_8")
    with pytest.raises(ValueError, match="Foundry"):
        validation.validate_rollout_model_renderer(body)


def test_policy_discriminator_whitespace_preserves_service_normalization(monkeypatch):
    monkeypatch.setattr(
        validation,
        "_load_model_catalog",
        lambda: {
            "Qwen/Qwen3.8-27B": ("qwen3_8",),
        },
    )
    body = _body()
    body.policy.type = " loom "
    validation.validate_rollout_model_renderer(body)


@pytest.mark.parametrize(
    "error", [OSError("catalog offline"), ValueError("invalid catalog")]
)
def test_catalog_failures_warn_and_defer_to_authoritative_service(monkeypatch, error):
    def unavailable():
        raise error

    monkeypatch.setattr(validation, "_load_model_catalog", unavailable)
    with pytest.warns(RuntimeWarning, match="The service will validate"):
        validation.validate_rollout_model_renderer(_body())


@pytest.mark.parametrize(
    "entries",
    [
        None,
        {},
        [],
        [None],
        [{"model": " model", "renderers": ["renderer"]}],
        [{"model": "", "renderers": ["renderer"]}],
        [{"model": "model", "renderers": []}],
        [{"model": "model", "renderers": [" "]}],
        [{"model": "model", "renderers": [" renderer"]}],
        [{"model": "model", "renderers": [None]}],
        [{"model": "model", "renderers": ["renderer", "renderer"]}],
        [{"model": "model", "renderers": ["renderer"]}] * 2,
    ],
)
def test_downloaded_catalog_is_strictly_validated(monkeypatch, entries):
    monkeypatch.setattr(validation, "_model_catalog", None)
    monkeypatch.setattr(
        validation,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(entries).encode()),
    )
    with pytest.raises(ValueError, match="catalog"):
        validation._load_model_catalog()


def test_catalog_download_is_bounded_and_cached_for_five_minutes(monkeypatch):
    now, requests = [100.0], []
    data = json.dumps([{"model": "model", "renderers": ["renderer"]}]).encode()

    def download(url, *, timeout):
        requests.append((url, timeout))
        return io.BytesIO(data)

    monkeypatch.setattr(validation, "_model_catalog", None)
    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(validation, "urlopen", download)
    assert validation._load_model_catalog() == {"model": ("renderer",)}
    now[0] += 299
    assert validation._load_model_catalog() == {"model": ("renderer",)}
    assert requests == [(validation._MODEL_CATALOG_URL, 10)]
    now[0] += 1
    validation._load_model_catalog()
    assert len(requests) == 2
    now[0] += 300
    monkeypatch.setattr(
        validation,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b"x" * (1024 * 1024 + 1)),
    )
    with pytest.raises(ValueError, match="1 MiB"):
        validation._load_model_catalog()


def test_catalog_outage_is_not_retried_for_each_rollout(monkeypatch):
    now, attempts = [100.0], []

    def unavailable(*_args, **_kwargs):
        attempts.append(True)
        raise OSError("catalog offline")

    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(validation, "urlopen", unavailable)
    for _ in range(2):
        with pytest.warns(RuntimeWarning, match="catalog offline"):
            validation.validate_rollout_model_renderer(_body())
    assert len(attempts) == 1
    now[0] += 30
    with pytest.warns(RuntimeWarning, match="catalog offline"):
        validation.validate_rollout_model_renderer(_body())
    assert len(attempts) == 2
