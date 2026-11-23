"""Tests for the OAuth proxy's encrypted token store (``storage.py``)."""

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from key_value.aio.protocols import AsyncKeyValue
from pydantic import SecretStr

from mcp_google_tasks import storage
from mcp_google_tasks.config import Settings
from mcp_google_tasks.errors import ConfigurationError
from mcp_google_tasks.storage import build_client_storage

from .conftest import SettingsFactory

FERNET_KEY = Fernet.generate_key().decode()
OTHER_FERNET_KEY = Fernet.generate_key().decode()
JWT_KEY = "test-jwt-signing-key"
REDIS_URL = "redis://:test-password@redis.example.com:6380/2"
SECRET_VALUE = "upstream-google-token-value"  # noqa: S105 - test placeholder


@pytest.fixture
def filetree_settings(make_settings: SettingsFactory, tmp_path: Path) -> Settings:
    """Oauth-proxy settings with an encrypted filetree store under ``tmp_path``."""
    return make_settings(
        "oauth-proxy",
        TOKEN_STORAGE_BACKEND="filetree",  # noqa: S106 - a backend name, not a secret
        TOKEN_STORAGE_DIR=str(tmp_path / "tokens"),
        STORAGE_ENCRYPTION_KEY=FERNET_KEY,
        JWT_SIGNING_KEY=JWT_KEY,
    )


def _store(settings: Settings) -> AsyncKeyValue:
    store = build_client_storage(settings)
    assert store is not None
    return store


def _files_under(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file()]


def test_default_backend_uses_fastmcp_storage(make_settings: SettingsFactory) -> None:
    assert build_client_storage(make_settings("oauth-proxy")) is None


async def test_filetree_round_trips_a_value(filetree_settings: Settings) -> None:
    store = _store(filetree_settings)

    await store.put("user-1", {"token": SECRET_VALUE}, collection="upstream")

    assert await store.get("user-1", collection="upstream") == {"token": SECRET_VALUE}


async def test_filetree_creates_missing_directory(
    make_settings: SettingsFactory, tmp_path: Path
) -> None:
    directory = tmp_path / "nested" / "does-not-exist"
    settings = make_settings(
        "oauth-proxy",
        TOKEN_STORAGE_BACKEND="filetree",  # noqa: S106 - a backend name, not a secret
        TOKEN_STORAGE_DIR=str(directory),
        STORAGE_ENCRYPTION_KEY=FERNET_KEY,
        JWT_SIGNING_KEY=JWT_KEY,
    )

    build_client_storage(settings)

    assert directory.is_dir()


async def test_filetree_encrypts_values_at_rest(filetree_settings: Settings) -> None:
    store = _store(filetree_settings)

    await store.put("user-1", {"token": SECRET_VALUE}, collection="upstream")

    files = _files_under(filetree_settings.storage_dir)
    assert files
    assert all(SECRET_VALUE not in f.read_text(encoding="utf-8") for f in files)


async def test_rotated_key_reads_as_a_cache_miss(filetree_settings: Settings) -> None:
    await _store(filetree_settings).put("user-1", {"token": SECRET_VALUE}, collection="c")
    rotated = dataclasses.replace(
        filetree_settings,
        storage_encryption_key=SecretStr(OTHER_FERNET_KEY),
    )

    assert await _store(rotated).get("user-1", collection="c") is None


async def test_prefix_isolates_collections(filetree_settings: Settings) -> None:
    first = _store(dataclasses.replace(filetree_settings, storage_key_prefix="tenant-a"))
    second = _store(dataclasses.replace(filetree_settings, storage_key_prefix="tenant-b"))
    unprefixed = _store(filetree_settings)

    await first.put("user-1", {"token": SECRET_VALUE}, collection="upstream")

    assert await first.get("user-1", collection="upstream") == {"token": SECRET_VALUE}
    assert await second.get("user-1", collection="upstream") is None
    assert await unprefixed.get("user-1", collection="upstream") is None


def test_redis_backend_builds_without_connecting(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real_redis_store = storage.RedisStore

    def recording_redis_store(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real_redis_store(**kwargs)

    monkeypatch.setattr(storage, "RedisStore", recording_redis_store)
    settings = make_settings(
        "oauth-proxy",
        TOKEN_STORAGE_BACKEND="redis",  # noqa: S106 - a backend name, not a secret
        REDIS_URL=REDIS_URL,
        STORAGE_ENCRYPTION_KEY=FERNET_KEY,
        JWT_SIGNING_KEY=JWT_KEY,
        STORAGE_KEY_PREFIX="tenant-a",
    )

    store = build_client_storage(settings)

    assert store is not None
    assert calls == [{"url": REDIS_URL}]


@pytest.mark.parametrize(
    ("backend", "change", "message"),
    [
        ("filetree", {"storage_encryption_key": None}, "STORAGE_ENCRYPTION_KEY is required"),
        ("redis", {"storage_encryption_key": None}, "STORAGE_ENCRYPTION_KEY is required"),
        ("redis", {"redis_url": None}, "REDIS_URL is required"),
    ],
)
def test_missing_backend_setting_is_a_configuration_error(
    make_settings: SettingsFactory,
    tmp_path: Path,
    backend: str,
    change: dict[str, Any],
    message: str,
) -> None:
    # load_settings rejects these combinations in oauth-proxy mode; build the
    # invalid settings directly to check the builder's own guard.
    valid = make_settings(
        "oauth-proxy",
        TOKEN_STORAGE_BACKEND=backend,
        TOKEN_STORAGE_DIR=str(tmp_path),
        REDIS_URL=REDIS_URL,
        STORAGE_ENCRYPTION_KEY=FERNET_KEY,
        JWT_SIGNING_KEY=JWT_KEY,
    )
    settings = dataclasses.replace(valid, **change)

    with pytest.raises(ConfigurationError, match=message):
        build_client_storage(settings)


@pytest.mark.parametrize("bad_key", ["not-a-fernet-key", "c2hvcnQ="])
def test_bad_fernet_key_is_rejected_at_load(make_settings: SettingsFactory, bad_key: str) -> None:
    with pytest.raises(ConfigurationError, match="not a valid Fernet key"):
        make_settings(
            "oauth-proxy",
            TOKEN_STORAGE_BACKEND="filetree",  # noqa: S106 - a backend name, not a secret
            STORAGE_ENCRYPTION_KEY=bad_key,
            JWT_SIGNING_KEY=JWT_KEY,
        )
