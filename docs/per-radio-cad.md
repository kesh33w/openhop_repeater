# Per-radio CAD calibration

The CAD page lists physical receivers with their frequency, spreading factor, and bandwidth. Select the receiver before starting a calibration, manual check, or saving settings. Selection is locked while a request or calibration is in progress. Changing receivers clears the previous result so it cannot be saved to the wrong device.

The read-only `GET /api/cad_radios` endpoint lists current radio settings and the active calibration receiver. `cad_calibration_start`, `cad_manual_check`, and `save_cad_settings` accept `radio_id` in their JSON body. `cad_calibration_stop` accepts an optional body with that ID and refuses to stop a different receiver's session. `cad_calibration_stream` accepts a `radio_id` query parameter and replays only that receiver's latest session. Every session event identifies its receiver. Unknown IDs fail instead of falling back to another radio.

Requests without a radio ID retain the default-radio behavior. Legacy bodyless Stop requests still work. There is one calibration session at a time; outstanding CAD checks block new CAD operations until they finish or cancellation completes.

Saving writes the selected `radios[].radio.cad` settings. Saving the default receiver also synchronizes the legacy top-level `radio.cad` block without changing settings inherited by other receivers. Startup applies each receiver's own settings rather than copying the default thresholds to all radios.

Opening the page or listing radios does not perform CAD. Calibration and manual checks operate on live hardware and can change its temporary CAD thresholds. Only run known-signal calibration when compatible traffic on the selected frequency, SF, BW, and CR is available. Deployment verification should use read-only discovery and passive logs unless a live test is explicitly planned.
