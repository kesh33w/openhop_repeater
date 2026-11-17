# CAD on/off per radio

The CAD Calibration page exposes **CAD before transmit** for each supported
USB/TCP modem. Existing settings remain unchanged until a user chooses a mode.
Turning it off skips channel-activity checks before transmit and can increase
collisions. It does not disable receive or issue a test transmission.

`POST /api/set_cad_enabled` accepts `{"radio_id":"bay","enabled":false}`.
The modem must acknowledge control before the API changes host CAD state. A
successful mode is saved under that physical radio's `radio.cad.enabled`, applied
at startup/reconnect, and retained when calibration thresholds are saved. Other
radios are not changed. Firmware that lacks acknowledged mode control returns an
error, not a successful-looking toggle.

`GET /api/cad_radios` reports `supports_cad_toggle`, `cad_enabled` (host state),
and `cad_mode_explicit`. When explicit is false, firmware's legacy auto-CAD state
is unmanaged; do not interpret host state as verified firmware state.
