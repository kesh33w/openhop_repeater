"""Resolve CAD operations to a physical radio, never the fabric's default proxy."""

from repeater.radio_ids import canonical_radio_id, configured_radio_ids


def physical_radios(daemon):
    radio = getattr(daemon, "radio", None)
    if radio is None:
        return {}, None
    root = getattr(radio, "_radio", radio)
    fabric = getattr(root, "fabric", None)
    if fabric is not None and getattr(fabric, "radios", None):
        return dict(fabric.radios), fabric.default_radio_id
    meta = getattr(daemon, "radio_stack_meta", {}) or {}
    radio_id = meta.get("default_radio") or "radio0"
    return {radio_id: root}, radio_id


def resolve_cad_radio(daemon, radio_id=None):
    radios, default_id = physical_radios(daemon)
    if radio_id is None or (isinstance(radio_id, str) and not radio_id.strip()):
        radio_id = default_id
    if not isinstance(radio_id, str) or radio_id not in radios:
        raise ValueError(f"Unknown or unavailable radio_id={radio_id!r}")
    radio = radios[radio_id]
    if not callable(getattr(radio, "perform_cad", None)):
        raise ValueError(f"Radio {radio_id!r} does not support CAD")  # noqa: TRY004 - capability validation
    return radio_id, radio


def radio_config(config, radio_id):
    """Effective radio settings using the stack builder's shallow overlay rules."""
    base = config.get("radio", {}) or {}
    configured_radio_ids(config)
    for entry in config.get("radios", []) or []:
        if canonical_radio_id(entry) == radio_id:
            return {**base, **(entry.get("radio", {}) or {})}
    return dict(base)


def apply_cad_settings(daemon):
    """Apply each device's persisted settings when the daemon starts."""
    radios, _ = physical_radios(daemon)
    applied = []
    for radio_id, radio in radios.items():
        cad = radio_config(daemon.config, radio_id).get("cad", {}) or {}
        peak, minimum = cad.get("peak_threshold", 23), cad.get("min_threshold", 11)
        try:
            symbols = int(cad.get("symbol_num", 2))
        except (TypeError, ValueError):
            symbols = 2
        if symbols not in {1, 2, 4, 8, 16}:
            symbols = 2
        if hasattr(radio, "set_custom_cad_thresholds"):
            radio.set_custom_cad_thresholds(peak=peak, min_val=minimum)
            if hasattr(radio, "set_custom_cad_symbol_num"):
                radio.set_custom_cad_symbol_num(symbols)
            applied.append((radio_id, peak, minimum, symbols))
    return applied
