import hashlib
from cryptography.fernet import Fernet
from app.core.config import settings

fernet = Fernet(settings.FERNET_KEY)


def hash_phone(phone: str) -> str:
    """SHA-256 hash of the normalised phone + salt (used as the unique user key)."""
    return hashlib.sha256(
        f"{phone}{settings.PHONE_HASH_SALT}".encode()
    ).hexdigest()


def encrypt_phone(phone: str) -> str:
    """Reversible (app-side) encryption of the phone number for storage."""
    return fernet.encrypt(phone.encode()).decode()


def decrypt_phone(encrypted_phone: str) -> str:
    return fernet.decrypt(encrypted_phone.encode()).decode()
