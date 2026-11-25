"""Save and load the fleet setup (drones, CA bundle path, script) as a local text file. No Streamlit.

The file (default ``fleet_ui/drones.txt``, override with ``FLEET_UI_DRONES_FILE``) is JSON. IPs, ports,
the CA bundle path and the script are stored as plain text. API keys are secrets and are never written
in clear text: they are stored only if a passphrase is given, encrypted with AES-256-GCM using a key
derived from the passphrase with scrypt. The passphrase itself is never stored.

The file is untrusted input when loading: size, types and lengths are checked, and only JSON is parsed.
"""
import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

logger = logging.getLogger("fleet_ui")

FORMAT_VERSION = 1
DEFAULT_FILE = Path(__file__).resolve().parent / "drones.txt"

MAX_FILE_BYTES = 1_000_000
MAX_DRONES = 50
MAX_SCRIPT_ROWS = 500
MAX_CELL_CHARS = 80
MAX_PATH_CHARS = 1024
MAX_KEY_CHARS = 256
MIN_PASSPHRASE_CHARS = 8
MAX_PASSPHRASE_CHARS = 256

KDF_NAME = "scrypt-n32768-r8-p1"
CIPHER_NAME = "AES-256-GCM"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SALT_BYTES, NONCE_BYTES = 16, 12
MAX_BLOB_BYTES = 100_000
AAD = b"tello-fleet-ui-keys-v1"


class StorageError(Exception):
    """The file or an argument is not usable (message is safe to show to the operator)."""


class WrongPassphrase(StorageError):
    """Decryption failed: wrong passphrase or the encrypted keys were modified."""


@dataclass(frozen=True)
class SavedSetup:
    drones: List[Tuple[str, str]]          # (ip, port) as typed
    ca_bundle: str
    script: List[str]                      # one text per row
    encrypted_keys: Optional[dict] = None  # opaque, pass to decrypt_keys()


def drones_file() -> Path:
    override = os.environ.get("FLEET_UI_DRONES_FILE")
    return Path(override) if override else DEFAULT_FILE


# ----- encryption of the API keys -----
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text, name: str, exact: Optional[int] = None) -> bytes:
    if not isinstance(text, str) or len(text) > MAX_BLOB_BYTES * 2:
        raise StorageError("encrypted keys are malformed ({})".format(name))
    try:
        data = base64.b64decode(text, validate=True)
    except ValueError:
        raise StorageError("encrypted keys are malformed ({})".format(name)) from None
    if exact is not None and len(data) != exact:
        raise StorageError("encrypted keys are malformed ({})".format(name))
    return data


def encrypt_keys(keys: Sequence[str], passphrase: str) -> dict:
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(list(keys)).encode("utf-8")
    data = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, plaintext, AAD)
    return {"cipher": CIPHER_NAME, "kdf": KDF_NAME,
            "salt": _b64(salt), "nonce": _b64(nonce), "data": _b64(data)}


def decrypt_keys(blob: dict, passphrase: str) -> List[str]:
    """Return the saved API keys, in the order of the saved drones. Raises StorageError."""
    if not isinstance(blob, dict) or blob.get("cipher") != CIPHER_NAME or blob.get("kdf") != KDF_NAME:
        raise StorageError("encrypted keys use an unsupported format")
    salt = _unb64(blob.get("salt"), "salt", SALT_BYTES)
    nonce = _unb64(blob.get("nonce"), "nonce", NONCE_BYTES)
    data = _unb64(blob.get("data"), "data")
    if not 0 < len(data) <= MAX_BLOB_BYTES:
        raise StorageError("encrypted keys are malformed (data)")
    if not isinstance(passphrase, str) or not 0 < len(passphrase) <= MAX_PASSPHRASE_CHARS:
        raise WrongPassphrase("wrong passphrase")
    try:
        plaintext = AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, data, AAD)
        keys = json.loads(plaintext)
    except InvalidTag:
        raise WrongPassphrase("wrong passphrase") from None
    except ValueError:
        raise StorageError("encrypted keys are malformed") from None
    if (not isinstance(keys, list) or len(keys) > MAX_DRONES
            or not all(isinstance(k, str) and len(k) <= MAX_KEY_CHARS for k in keys)):
        raise StorageError("encrypted keys are malformed")
    return keys


# ----- save -----
def save_setup(path: Path, drones: Sequence[Tuple[str, str, str]], ca_bundle: str,
               script: Sequence[str], passphrase: str = "") -> bool:
    """Write the setup. ``drones`` are (ip, port, api_key). Returns True if the keys were saved
    (encrypted), False if they were left out because no passphrase was given."""
    if passphrase and not MIN_PASSPHRASE_CHARS <= len(passphrase) <= MAX_PASSPHRASE_CHARS:
        raise StorageError("passphrase must be {}-{} characters".format(MIN_PASSPHRASE_CHARS,
                                                                       MAX_PASSPHRASE_CHARS))
    if len(drones) > MAX_DRONES or len(script) > MAX_SCRIPT_ROWS:
        raise StorageError("too many drones or script rows to save")
    document = {
        "version": FORMAT_VERSION,
        "drones": [{"ip": ip, "port": port} for ip, port, _key in drones],
        "ca_bundle": ca_bundle,
        "script": list(script),
    }
    if passphrase:
        document["encrypted_keys"] = encrypt_keys([key for _ip, _port, key in drones], passphrase)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    _write_private(Path(path), text)
    return bool(passphrase)


def _write_private(path: Path, text: str) -> None:
    """Atomic write with owner-only permissions (the file may hold encrypted keys)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".drones-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:     # mkstemp creates it with mode 0600
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


# ----- load -----
def _text_list(value, limit: int, max_chars: int, what: str) -> List[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise StorageError("'{}' is missing or too long".format(what))
    if not all(isinstance(item, str) and len(item) <= max_chars for item in value):
        raise StorageError("'{}' contains invalid entries".format(what))
    return value


def load_setup(path: Path) -> Tuple[Optional[SavedSetup], Optional[str]]:
    """Returns (setup, None) on success, (None, None) if the file does not exist, and
    (None, message) if it exists but cannot be used."""
    path = Path(path)
    try:
        if not path.is_file():
            return None, None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, "{} is too large".format(path.name)
        raw = path.read_bytes()
    except OSError:
        logger.exception("could not read %s", path)
        return None, "{} could not be read".format(path.name)
    try:
        return _parse(raw), None
    except StorageError as exc:
        return None, "{} is not valid: {}".format(path.name, exc)


def _parse(raw: bytes) -> SavedSetup:
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise StorageError("not a JSON file") from None
    if not isinstance(document, dict) or document.get("version") != FORMAT_VERSION:
        raise StorageError("unsupported format version")

    entries = document.get("drones")
    if not isinstance(entries, list) or len(entries) > MAX_DRONES:
        raise StorageError("'drones' is missing or too long")
    drones = []
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("ip"), str)
                or not isinstance(entry.get("port"), str)
                or len(entry["ip"]) > 15 or len(entry["port"]) > 5):
            raise StorageError("'drones' contains invalid entries")
        drones.append((entry["ip"], entry["port"]))

    ca_bundle = document.get("ca_bundle", "")
    if not isinstance(ca_bundle, str) or len(ca_bundle) > MAX_PATH_CHARS:
        raise StorageError("'ca_bundle' is invalid")
    script = _text_list(document.get("script", []), MAX_SCRIPT_ROWS, MAX_CELL_CHARS, "script")

    blob = document.get("encrypted_keys")
    if blob is not None and not isinstance(blob, dict):
        raise StorageError("'encrypted_keys' is invalid")
    return SavedSetup(drones, ca_bundle, script, blob)
