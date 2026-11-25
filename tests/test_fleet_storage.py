import json
import os
import stat

import pytest

from fleet_ui import storage
from fleet_ui.storage import StorageError, WrongPassphrase

KEY1 = "key-one-" + "x" * 30
KEY2 = "key-two-" + "y" * 30
DRONES = [("192.168.4.11", "8000", KEY1), ("192.168.4.12", "8001", KEY2)]
SCRIPT = ["takeoff", "up 50", "# comment", "land"]
PASSPHRASE = "correct horse battery"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "drones.txt"


def test_round_trip_with_keys(path):
    assert storage.save_setup(path, DRONES, "/certs/ca.pem", SCRIPT, PASSPHRASE) is True
    setup, error = storage.load_setup(path)
    assert error is None
    assert setup.drones == [("192.168.4.11", "8000"), ("192.168.4.12", "8001")]
    assert setup.ca_bundle == "/certs/ca.pem" and setup.script == SCRIPT
    assert storage.decrypt_keys(setup.encrypted_keys, PASSPHRASE) == [KEY1, KEY2]


def test_api_keys_are_never_in_the_file_in_clear_text(path):
    storage.save_setup(path, DRONES, "", SCRIPT, PASSPHRASE)
    raw = path.read_text()
    assert KEY1 not in raw and KEY2 not in raw and PASSPHRASE not in raw
    assert "encrypted_keys" in raw


def test_without_passphrase_keys_are_left_out(path):
    assert storage.save_setup(path, DRONES, "", SCRIPT, "") is False
    raw = path.read_text()
    assert KEY1 not in raw and "encrypted_keys" not in raw
    setup, error = storage.load_setup(path)
    assert error is None and setup.encrypted_keys is None
    assert setup.drones == [("192.168.4.11", "8000"), ("192.168.4.12", "8001")]


def test_file_is_owner_only_and_valid_json(path):
    storage.save_setup(path, DRONES, "", SCRIPT, PASSPHRASE)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == storage.FORMAT_VERSION
    assert [p.name for p in path.parent.iterdir()] == ["drones.txt"]      # no temp files left behind


def test_overwrite_replaces_previous_content(path):
    storage.save_setup(path, DRONES, "", SCRIPT, PASSPHRASE)
    storage.save_setup(path, DRONES[:1], "", ["land"], "")
    setup, _ = storage.load_setup(path)
    assert setup.drones == [("192.168.4.11", "8000")] and setup.script == ["land"]
    assert setup.encrypted_keys is None


def test_unicode_script_round_trip(path):
    storage.save_setup(path, DRONES[:1], "/certs/\u00e9.pem", ["# l\u00e4nd"], "")
    setup, _ = storage.load_setup(path)
    assert setup.script == ["# l\u00e4nd"] and setup.ca_bundle == "/certs/\u00e9.pem"


def test_wrong_passphrase_is_rejected(path):
    storage.save_setup(path, DRONES, "", SCRIPT, PASSPHRASE)
    setup, _ = storage.load_setup(path)
    for bad in ("wrong passphrase", "", "x" * 1000):
        with pytest.raises(WrongPassphrase):
            storage.decrypt_keys(setup.encrypted_keys, bad)


def test_tampered_ciphertext_is_rejected(path):
    storage.save_setup(path, DRONES, "", SCRIPT, PASSPHRASE)
    document = json.loads(path.read_text())
    data = bytearray(__import__("base64").b64decode(document["encrypted_keys"]["data"]))
    data[0] ^= 1
    document["encrypted_keys"]["data"] = __import__("base64").b64encode(bytes(data)).decode()
    with pytest.raises(WrongPassphrase):
        storage.decrypt_keys(document["encrypted_keys"], PASSPHRASE)


def test_each_save_uses_a_fresh_salt_and_nonce():
    a = storage.encrypt_keys([KEY1], PASSPHRASE)
    b = storage.encrypt_keys([KEY1], PASSPHRASE)
    assert a["salt"] != b["salt"] and a["nonce"] != b["nonce"] and a["data"] != b["data"]


@pytest.mark.parametrize("blob", [
    None, [], {}, {"cipher": "AES-256-GCM"}, {"cipher": "ROT13", "kdf": storage.KDF_NAME},
    {"cipher": storage.CIPHER_NAME, "kdf": "md5"},
    {"cipher": storage.CIPHER_NAME, "kdf": storage.KDF_NAME, "salt": "!!!", "nonce": "", "data": ""},
    {"cipher": storage.CIPHER_NAME, "kdf": storage.KDF_NAME, "salt": "AAAA", "nonce": "AAAA", "data": "AAAA"},
])
def test_malformed_encrypted_blob(blob):
    with pytest.raises(StorageError):
        storage.decrypt_keys(blob, PASSPHRASE)


def test_passphrase_length_rules_on_save(path):
    with pytest.raises(StorageError):
        storage.save_setup(path, DRONES, "", SCRIPT, "short")
    assert not path.exists()
    with pytest.raises(StorageError):
        storage.save_setup(path, DRONES, "", SCRIPT, "x" * 1000)


def test_save_limits(path):
    with pytest.raises(StorageError):
        storage.save_setup(path, DRONES, "", ["land"] * (storage.MAX_SCRIPT_ROWS + 1), "")
    with pytest.raises(StorageError):
        storage.save_setup(path, DRONES * 30, "", SCRIPT, "")


def test_missing_file_is_not_an_error(path):
    assert storage.load_setup(path) == (None, None)


@pytest.mark.parametrize("content", [
    "not json", "[]", "null", '{"version": 2, "drones": []}', '{"drones": []}',
    '{"version": 1}', '{"version": 1, "drones": "x"}',
    '{"version": 1, "drones": [1]}',
    '{"version": 1, "drones": [{"ip": 5, "port": "1"}]}',
    '{"version": 1, "drones": [{"ip": "1.1.1.1", "port": 1}]}',
    '{"version": 1, "drones": [{"ip": "' + "1" * 40 + '", "port": "1"}]}',
    '{"version": 1, "drones": [], "ca_bundle": 7}',
    '{"version": 1, "drones": [], "script": "land"}',
    '{"version": 1, "drones": [], "script": [1]}',
    '{"version": 1, "drones": [], "script": ["' + "a" * 500 + '"]}',
    '{"version": 1, "drones": [], "encrypted_keys": "x"}',
])
def test_invalid_files_are_reported_not_raised(path, content):
    path.write_text(content)
    setup, error = storage.load_setup(path)
    assert setup is None and error and "drones.txt" in error


def test_binary_and_oversized_files(path):
    path.write_bytes(b"\xff\xfe\x00bad")
    assert storage.load_setup(path)[0] is None
    path.write_text(" " * (storage.MAX_FILE_BYTES + 1))
    setup, error = storage.load_setup(path)
    assert setup is None and "too large" in error


def test_unreadable_file_message_has_no_path_details(path):
    path.mkdir()                       # a directory is not a file: treated as missing
    assert storage.load_setup(path) == (None, None)


def test_drones_file_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_UI_DRONES_FILE", str(tmp_path / "x.txt"))
    assert storage.drones_file() == tmp_path / "x.txt"
    monkeypatch.delenv("FLEET_UI_DRONES_FILE")
    assert storage.drones_file() == storage.DEFAULT_FILE
    assert storage.DEFAULT_FILE.name == "drones.txt"
