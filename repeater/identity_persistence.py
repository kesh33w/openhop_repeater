"""Keep a loaded file-backed identity out of persisted configuration."""

import base64
import binascii
import copy
import logging
from pathlib import Path

logger = logging.getLogger("Config")


def _read_identity_file(path):
    """Decode a key file exactly as the loader does (lenient base64, wrapped lines OK)."""
    try:
        key = base64.b64decode(Path(path).read_bytes())
    except (OSError, binascii.Error, ValueError):
        return None
    return key if len(key) in (32, 64) else None


def config_for_persistence(config):
    """Return a copy of ``config`` that is safe to write to disk.

    When ``identity_key`` is just the in-memory copy of ``identity_file``, it is
    dropped so the private key is not duplicated into config.yaml.

    When the two differ (both set on purpose, a restored backup, or an
    unreadable file), the runtime key is kept. The loader gives ``identity_key``
    precedence, so this is the only way the identity in use survives a restart.
    Refusing to save instead would block every later configuration change.
    """
    candidate = copy.deepcopy(config)
    repeater = candidate.get("repeater") or {}
    identity_file = repeater.get("identity_file")
    if not identity_file or "identity_key" not in repeater:
        return candidate
    runtime_key = repeater["identity_key"]
    if isinstance(runtime_key, str):
        try:
            runtime_key = bytes.fromhex(runtime_key)
        except ValueError:
            return candidate
    if isinstance(runtime_key, (bytes, bytearray)) and _read_identity_file(identity_file) == bytes(
        runtime_key
    ):
        del repeater["identity_key"]
    else:
        logger.warning(
            "identity_key differs from identity_file (or the file is unreadable); "
            "keeping identity_key in the saved configuration so it wins on restart"
        )
    return candidate
