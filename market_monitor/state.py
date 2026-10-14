"""Pack/unpack the SQLite state for storage on the public `data` branch.

Format: b"MMS1" | 16-byte salt | Fernet token( gzip(state.db) )
- key = scrypt(STATE_ENCRYPTION_KEY, salt) -> Fernet (AES-128-CBC + HMAC-SHA256),
  so the file is both confidential and tamper-evident;
- the database is integrity-checked before packing and after unpacking: a broken
  database is never pushed and never silently used.
"""

from __future__ import annotations

import base64
import gzip
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"MMS1"
PLAIN_MAGIC = b"MMP1"  # unencrypted (local use only, needs explicit opt-in)
SALT_LEN = 16


class StateError(Exception):
    pass


def _fernet(passphrase: str, salt: bytes) -> Fernet:
    if len(passphrase) < 16:
        raise StateError("STATE_ENCRYPTION_KEY must be at least 16 characters")
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def check_database(path: str | Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise StateError(f"database unreadable: {exc}") from exc
    finally:
        conn.close()
    if result != "ok":
        raise StateError(f"database integrity check failed: {result}")


def pack(db_path: str | Path, out_path: str | Path, passphrase: str | None) -> int:
    """Write the packed state; returns its size in bytes."""
    check_database(db_path)
    data = gzip.compress(Path(db_path).read_bytes(), compresslevel=9, mtime=0)
    if passphrase:
        salt = os.urandom(SALT_LEN)
        blob = MAGIC + salt + _fernet(passphrase, salt).encrypt(data)
    else:
        blob = PLAIN_MAGIC + data
    tmp = Path(str(out_path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(blob)
    tmp.replace(out_path)
    return len(blob)


def unpack(in_path: str | Path, db_path: str | Path, passphrase: str | None) -> None:
    blob = Path(in_path).read_bytes()
    if blob.startswith(MAGIC):
        if not passphrase:
            raise StateError("state is encrypted but STATE_ENCRYPTION_KEY is not set")
        salt = blob[len(MAGIC):len(MAGIC) + SALT_LEN]
        try:
            data = _fernet(passphrase, salt).decrypt(blob[len(MAGIC) + SALT_LEN:])
        except InvalidToken:
            raise StateError("cannot decrypt state: wrong STATE_ENCRYPTION_KEY or corrupted file") from None
    elif blob.startswith(PLAIN_MAGIC):
        data = blob[len(PLAIN_MAGIC):]
    else:
        raise StateError("unknown state file format")
    tmp = Path(str(db_path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(gzip.decompress(data))
    check_database(tmp)
    tmp.replace(db_path)
