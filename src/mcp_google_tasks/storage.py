"""Persistent, encrypted storage for the OAuth proxy's upstream tokens.

Only ``oauth-proxy`` mode stores anything: FastMCP's OAuth proxy keeps each
user's upstream Google tokens and registered clients in this store. Values are
Fernet-encrypted before they reach the disk or Redis.
"""

from cryptography.fernet import Fernet
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

from mcp_google_tasks.config import Settings
from mcp_google_tasks.errors import ConfigurationError


def build_client_storage(settings: Settings) -> AsyncKeyValue | None:
    """Build the OAuth proxy's encrypted token store.

    Args:
        settings: Validated settings.

    Returns:
        ``None`` for the ``default`` backend (FastMCP's own storage); otherwise
        a Fernet-encrypted ``filetree`` or ``redis`` store, optionally prefixed.

    Raises:
        ConfigurationError: A setting the chosen backend needs is missing.
    """
    if settings.storage_backend == "default":
        return None
    if settings.storage_encryption_key is None:
        msg = "STORAGE_ENCRYPTION_KEY is required for persistent token storage."
        raise ConfigurationError(msg)

    base_store: AsyncKeyValue
    if settings.storage_backend == "filetree":
        directory = settings.storage_dir
        directory.mkdir(parents=True, exist_ok=True)
        base_store = FileTreeStore(
            data_directory=directory,
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
            collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
        )
    else:
        if settings.redis_url is None:
            msg = "REDIS_URL is required when TOKEN_STORAGE_BACKEND='redis'."
            raise ConfigurationError(msg)
        base_store = RedisStore(url=settings.redis_url.get_secret_value())

    store: AsyncKeyValue = base_store
    if settings.storage_key_prefix:
        store = PrefixCollectionsWrapper(key_value=store, prefix=settings.storage_key_prefix)

    # The key was validated by load_settings, so this cannot raise here.
    fernet = Fernet(settings.storage_encryption_key.get_secret_value().encode("utf-8"))
    # raise_on_decryption_error=False makes key rotation degrade to a cache miss
    # (the user signs in again) instead of an HTTP 500.
    return FernetEncryptionWrapper(key_value=store, fernet=fernet, raise_on_decryption_error=False)
