import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).parents[1] / "dist" / "rle" / "install_rle_sdk.py"
_SPEC = importlib.util.spec_from_file_location("rle_release_installer", _PATH)
installer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = installer
_SPEC.loader.exec_module(installer)


def _manifest():
    filename = "azure_ai_projects-2.7.0b2-py3-none-any.whl"
    return {
        "schema_version": 1, "package": "azure-ai-projects", "channel": "rle-dev",
        "latest": "2.7.0b2", "versions": {"2.7.0b2": {
            "filename": filename,
            "url": f"https://raw.githubusercontent.com/Azure/azure-sdk-for-python/rle-dev-releases/sdk/ai/azure-ai-projects/dist/rle/wheels/2.7.0b2/{filename}",
            "sha256": hashlib.sha256(b"wheel").hexdigest(),
        }},
    }


def test_resolve_latest_and_explicit():
    assert installer.resolve_release(_manifest()) == installer.resolve_release(_manifest(), "2.7.0b2")


@pytest.mark.parametrize("field,value", [
    ("sha256", "invalid"),
    ("filename", "../wheel.whl"),
    ("url", "https://example.com/wheel.whl"),
])
def test_reject_invalid_artifact(field, value):
    manifest = _manifest()
    manifest["versions"]["2.7.0b2"][field] = value
    with pytest.raises(ValueError):
        installer.resolve_release(manifest)


def test_checksum_fails_closed(tmp_path):
    release = installer.resolve_release(_manifest())
    wheel = tmp_path / release.filename
    wheel.write_bytes(b"wheel")
    installer.verify_checksum(wheel, release)
    wheel.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        installer.verify_checksum(wheel, release)
