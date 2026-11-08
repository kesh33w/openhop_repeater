"""Observer key compatibility using public test vectors, never operational keys."""

import base64
import hashlib
import json
import os
from unittest.mock import patch

import pytest
from nacl.signing import VerifyKey
from openhop_core import LocalIdentity

from repeater.data_acquisition.observer_identity import _load_seed, load_observer_identities
from tests.test_mqtt_radio_feeds import PUB, config, packet, rig

# RFC 8032 section 7.1, test 1. These public fixtures must never be deployed.
TEST_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
TEST_PUBLIC = "D75A980182B10AB7D54BFED3C964073A0EE172F3DAA62325AF021A68F707511A"
TEST_EMPTY_SIGNATURE = (
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)
expanded = bytearray(hashlib.sha512(TEST_SEED).digest())
expanded[0] &= 248
expanded[31] = (expanded[31] & 63) | 64
TEST_EXPANDED = bytes(expanded)
# Scalar 1 derives the Ed25519 base point. It catches accidental clamping,
# truncation, or seed reinterpretation of firmware input.
TEST_UNCLAMPED = b"\x01" + bytes(31) + b"\x03" * 32
TEST_BASE_POINT = "58" + "66" * 31


def write_key(tmp_path, contents):
    path = tmp_path / "observer.key"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
    return str(path)


def observer_config(path):
    settings = config()
    settings["mqtt_brokers"].update(
        separate_observer_identities=True,
        observer_identities={
            "host-rf": {"name": "Existing Sacramento", "use_repeater_identity": True},
            "bay": {"name": "Bay observer", "private_key_file": path},
        },
    )
    return settings


@pytest.mark.parametrize("private_key", [TEST_SEED, TEST_EXPANDED, TEST_UNCLAMPED])
@pytest.mark.parametrize("newline", [b"", b"\n", b"\r\n"])
@pytest.mark.parametrize("uppercase", [False, True])
def test_loader_preserves_both_key_formats(tmp_path, private_key, newline, uppercase):
    encoded = private_key.hex()
    if uppercase:
        encoded = encoded.upper()
    path = write_key(tmp_path, encoded.encode("ascii") + newline)
    assert _load_seed(path) == private_key


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"00" * 31,
        b"00" * 33,
        b"00" * 63,
        b"00" * 65,
        b" " + b"00" * 32,
        b"00" * 32 + b" ",
        b"00 " * 32,
        b"0x" + b"00" * 32,
        b"00" * 32 + b"\n\n",
        b"00" * 64 + b"\r",
        b"00" * 64 + b"\r\n\n",
        b"00" * 64 + b"\ntrailing-data",
        b"00" * 64 + b"\0",
        b"GG" * 32,
        b"\xff" * 64,
        TEST_SEED,
        TEST_EXPANDED,
    ],
)
def test_loader_rejects_noncanonical_content_without_echo(tmp_path, contents):
    path = write_key(tmp_path, contents)
    with pytest.raises(ValueError) as result:
        _load_seed(path)
    assert str(result.value) == (
        "observer key must contain a 32-byte hex seed or 64-byte MeshCore expanded key"
    )


@pytest.mark.parametrize("private_key", [TEST_SEED, TEST_EXPANDED])
def test_seed_and_firmware_forms_match_rfc_signature(tmp_path, private_key):
    path = write_key(tmp_path, private_key.hex().encode())
    identity = LocalIdentity(seed=_load_seed(path))
    assert identity.get_public_key().hex().upper() == TEST_PUBLIC
    assert identity.sign(b"").hex() == TEST_EMPTY_SIGNATURE


@pytest.mark.parametrize(
    "private_key,public_key,firmware",
    [
        (TEST_SEED, TEST_PUBLIC, False),
        (TEST_EXPANDED, TEST_PUBLIC, True),
        (TEST_UNCLAMPED, TEST_BASE_POINT, True),
    ],
)
def test_full_mqtt_path_preserves_identity_and_signs_jwt(
    tmp_path, private_key, public_key, firmware
):
    path = write_key(tmp_path, private_key.hex().encode() + b"\n")
    settings = observer_config(path)
    pusher, clients, messages = rig(settings)
    again, _, _ = rig(settings)
    sac, bay = pusher.connections
    assert sac.public_key == PUB
    assert bay.public_key == public_key
    assert again.connections[1].public_key == public_key
    assert bay.local_identity.get_signing_key_bytes() == private_key
    assert (bay.local_identity.signing_key is None) is firmware
    bay.use_jwt_auth = True
    bay.broker["audience"] = "broker.invalid"
    bay._set_credentials()
    credentials = clients[1].username_pw_set.call_args.kwargs
    assert credentials["username"] == f"v1_{public_key}"
    header, claims, signature = credentials["password"].split(".")
    body = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    assert body["publicKey"] == public_key
    VerifyKey(bytes.fromhex(public_key)).verify(
        f"{header}.{claims}".encode(), bytes.fromhex(signature)
    )
    pusher.publish_packet(packet("bay", ["host-rf"]))
    assert messages[-1][0] == f"meshcore/SFO/{public_key}/packets"
    assert messages[-1][1]["origin_id"] == public_key
    assert messages[-1][1]["origin"] == "Bay observer"
    for state in ("online", "offline"):
        pusher.publish_status(state=state)
        assert messages[-1][0] == f"meshcore/SFO/{public_key}/status"
        assert messages[-1][1]["origin_id"] == public_key
        assert messages[-1][1]["status"] == state
    assert all(not client.connect.called for client in clients)


def test_invalid_firmware_scalar_blocks_only_its_feed(tmp_path):
    path = write_key(tmp_path, b"00" * 64)
    pusher, clients, _ = rig(observer_config(path))
    sac, bay = pusher.connections
    assert sac.enabled and sac.public_key == PUB
    assert not bay.enabled and bay.local_identity is None
    assert bay.identity_error == "observer key is not a valid Ed25519 private key"
    bay.connect()
    assert all(not client.connect.called for client in clients)


def test_constructor_error_cannot_expose_key_material(tmp_path):
    path = write_key(tmp_path, TEST_EXPANDED.hex().encode())
    rf = LocalIdentity(seed=b"\x03" * 32)
    with patch("openhop_core.LocalIdentity", side_effect=RuntimeError(TEST_EXPANDED.hex())):
        identities, errors = load_observer_identities(observer_config(path), rf)
    assert set(identities) == {"host-rf"}
    assert errors == {"bay": "observer key is not a valid Ed25519 private key"}


def test_seed_and_expanded_duplicate_identity_still_rejected(tmp_path):
    path = write_key(tmp_path, TEST_EXPANDED.hex().encode())
    rf = LocalIdentity(seed=TEST_SEED)
    identities, errors = load_observer_identities(observer_config(path), rf)
    assert set(identities) == {"host-rf"}
    assert errors == {
        "bay": "observer key must differ from the RF repeater key; select it explicitly"
    }
