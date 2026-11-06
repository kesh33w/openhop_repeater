import base64
import copy

import pytest
import yaml

from repeater.config import save_config
from repeater.config_manager import ConfigManager


@pytest.mark.parametrize("save_path", ["manager", "legacy"])
def test_file_identity_is_not_duplicated_in_saved_config(tmp_path, save_path):
    key = bytes(range(32))
    key_file = tmp_path / "identity.key"
    key_file.write_bytes(base64.b64encode(key))
    key_file.chmod(0o600)
    config_file = tmp_path / "config.yaml"
    initial = {"repeater": {"identity_file": str(key_file), "mode": "forward"}}
    config_file.write_text(yaml.safe_dump(initial))
    config = copy.deepcopy(initial)
    config["repeater"]["identity_key"] = key
    config["radio_type"] = "null"
    before = copy.deepcopy(config)

    def save():
        if save_path == "manager":
            return ConfigManager(str(config_file), config).save_to_file()
        return save_config(config, str(config_file))

    assert save()
    saved = yaml.safe_load(config_file.read_text())
    assert saved["repeater"]["identity_file"] == str(key_file)
    assert "identity_key" not in saved["repeater"]
    assert config == before

    previous_bytes = config_file.read_bytes()
    config["repeater"]["identity_key"] = bytes(reversed(range(32)))
    assert not save()
    assert config_file.read_bytes() == previous_bytes
