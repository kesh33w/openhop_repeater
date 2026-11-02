"""Canonical configured radio names shared by routing and configuration views."""


def canonical_radio_id(entry, *, context="radios[] entry"):
    """Resolve the established id/radio_id aliases without guessing a destination."""
    if not isinstance(entry, dict):
        raise ValueError(f"{context} must be a mapping")  # noqa: TRY004 - configuration validation contract
    names = []
    for field in ("id", "radio_id"):
        value = entry.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or value != value.strip() or not value:
            raise ValueError(f"{context}.{field} must be a nonempty, unpadded string")
        names.append(value)
    if not names:
        raise ValueError(f"{context} requires an 'id' or 'radio_id'")
    if len(set(names)) != 1:
        raise ValueError(f"{context} has conflicting 'id' and 'radio_id'; use one radio name")
    return names[0]


def configured_radio_ids(config):
    """Validate all names before hardware opens; no radios list means legacy mode."""
    entries = config.get("radios")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ValueError("radios must be a list of named radio mappings")  # noqa: TRY004 - configuration validation contract
    names = []
    for index, entry in enumerate(entries):
        name = canonical_radio_id(entry, context=f"radios[{index}]")
        if name in names:
            raise ValueError(f"radios[{index}] duplicates radio ID {name!r}; use distinct names")
        names.append(name)
    return tuple(names)
