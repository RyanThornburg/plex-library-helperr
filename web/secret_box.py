"""secret_box.py - encrypts the config's secrets (api keys, tokens) before they're stored,
with a key from HELPERR_SECRET_KEY, so a copy of the database doesn't expose them"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# the env var is random (e.g. openssl rand -hex 32), so hkdf is enough to make a key of it
MIN_KEY_CHARS = 32


class SecretError(Exception):
    """a stored secret can't be decrypted, e.g. HELPERR_SECRET_KEY changed"""


class SecretBox:
    def __init__(self, key_material: str):
        if len(key_material) < MIN_KEY_CHARS:
            raise ValueError(f"the secret key must be at least {MIN_KEY_CHARS} characters")
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"helperr config secrets"
        ).derive(key_material.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            raise SecretError(
                "can't be decrypted, HELPERR_SECRET_KEY may have changed"
            ) from None
