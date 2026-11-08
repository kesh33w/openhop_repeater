"""Load observer-only identities without creating or registering RF identities."""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ObserverIdentity:
    name: str
    identity: Any

    @property
    def public_key(self):
        return self.identity.get_public_key().hex().upper()


def _load_seed(path):
    """Read a hex Ed25519 seed or MeshCore expanded key without changing its bytes."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("private_key_file must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("observer key must be a regular file")
            if metadata.st_mode & 0o077:
                raise ValueError("observer key must not be accessible to group or others")
            # Longest accepted file is 128 hex characters plus CRLF. Read one
            # extra byte so an oversized file cannot pass via a valid prefix.
            raw = stream.read(131)
    except OSError:
        raise ValueError("observer key file is missing, unreadable, or a symbolic link") from None
    if re.fullmatch(rb"(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{128})(?:\r?\n)?", raw) is None:
        raise ValueError(
            "observer key must contain a 32-byte hex seed or 64-byte MeshCore expanded key"
        )
    return bytes.fromhex(raw.decode("ascii"))


def load_observer_identities(config, repeater_identity):
    """Return valid identities and safe errors, never generate or replace a key.

    Configuration errors disable all separate feeds. File errors disable the
    affected radio. Duplicate public keys disable both radios, not just the
    later one. These identities never enter the RF identity manager.
    """
    from repeater.config import validate_observer_identity_config

    try:
        validate_observer_identity_config(config)
    except ValueError as exc:
        return {}, {"*": str(exc)}
    from openhop_core import LocalIdentity

    repeater_public_key = repeater_identity.get_public_key().hex().upper()
    configured = config.get("mqtt_brokers", {}).get("observer_identities", {})
    identities, errors, owners = {}, {}, {}
    for radio_id, spec in configured.items():
        try:
            use_repeater = spec.get("use_repeater_identity") is True
            if use_repeater:
                signer = repeater_identity
            else:
                private_key = _load_seed(spec["private_key_file"])
                try:
                    signer = LocalIdentity(seed=private_key)
                except Exception:
                    # Invalid expanded scalars can raise a crypto-library
                    # exception. Block this feed without exposing key material
                    # or preventing the RF repeater from starting.
                    raise ValueError("observer key is not a valid Ed25519 private key") from None
            identity = ObserverIdentity(spec["name"], signer)
            key = identity.public_key
            if not use_repeater and key == repeater_public_key:
                raise ValueError(
                    "observer key must differ from the RF repeater key; select it explicitly"
                )
            if key in owners:
                other = owners[key]
                errors[other] = "observer key is shared by more than one radio"
                identities.pop(other, None)
                raise ValueError("observer key is shared by more than one radio")
            owners[key] = radio_id
            identities[radio_id] = identity
        except ValueError as exc:
            errors[radio_id] = str(exc)
    return identities, errors
