"""Two transmitters and three shadow ears need no four-radio special case."""
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from repeater.config import validate_fabric_config
from tests.test_channel_bridge import packet
from tests.test_network_topology import Rig, config


@pytest.mark.asyncio
async def test_five_radio_layout_preserves_shadow_isolation_and_comparison(tmp_path):
    cfg = config(tmp_path)
    rak = copy.deepcopy(cfg["radios"][3])
    rak["id"] = "rak910-shadow"
    cfg["radios"].append(rak)
    validate_fabric_config(cfg)
    rig = Rig(cfg)
    try:
        dispatch = AsyncMock()
        rig.dispatcher._dispatch = dispatch
        raw = packet().write_to()
        shadows = ("omni909", "omni910", "rak910-shadow")
        for ear in shadows:
            rig.ears[ear].rx_callback(raw, -90, 8)
            assert rig.topology.destinations(ear) == ()
            with pytest.raises(ValueError):
                await rig.radio.send(raw, radio_id=ear)
            for target in ("local", "link"):
                with pytest.raises(ValueError):
                    await rig.radio.send(raw, radio_id=target, rx_radio_id=ear)
                with pytest.raises(ValueError):
                    await rig.radio.send(raw, radio_id=target, reply_to_radio_id=ear)
        await asyncio.sleep(0)
        dispatch.assert_not_called()
        assert rig.air.frames == []
        assert set(rig.radio.fabric.radios) == {"local", "link", *shadows}
        snapshot = rig.radio.fabric.comparison.snapshot()
        assert snapshot["networks"] == {"909": 1, "910": 1}
        assert snapshot["receivers"]["rak910-shadow"]["unique_packets"] == 1
        assert snapshot["receivers"]["omni910"]["exclusive_packets"] == 0
        rig.handler.config["fabric"]["origin_tx"] = "all"
        assert rig.handler._resolve_origin_tx_radio_ids() == ("local", "link")
        rig.radio.fabric.unregister_radio("omni910")
        assert rig.radio.resolve_tx_radio_id(raw, rx_radio_id="local") == "local"
        assert rig.radio.resolve_tx_radio_id(raw, rx_radio_id="link") == "link"
    finally:
        rig.close()
