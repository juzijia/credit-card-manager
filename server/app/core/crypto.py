import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr


def _key(master_key: SecretStr | None) -> bytes:
    if master_key is None:
        raise ValueError("CCM_MASTER_KEY is required for encrypted account secrets")
    try:
        key = base64.urlsafe_b64decode(master_key.get_secret_value())
    except Exception as exc:
        raise ValueError("CCM_MASTER_KEY must be URL-safe Base64") from exc
    if len(key) != 32:
        raise ValueError("CCM_MASTER_KEY must decode to exactly 32 bytes")
    return key


def encrypt_secret(value: SecretStr, master_key: SecretStr | None) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(master_key)).encrypt(nonce, value.get_secret_value().encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_secret(value: str, master_key: SecretStr | None) -> SecretStr:
    payload = base64.urlsafe_b64decode(value)
    clear = AESGCM(_key(master_key)).decrypt(payload[:12], payload[12:], None)
    return SecretStr(clear.decode())
