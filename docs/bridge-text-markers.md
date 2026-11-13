# Bridge text markers

Each directional channel mapping accepts an optional `text_prefix`. For example:

```yaml
channel_bridge:
  mappings:
    - source_radio: host-rf
      target_radio: bay
      source_channel: Public
      target_channel: "#910public"
      text_prefix: "↔"
```

Add the field to existing mappings without replacing the rest of the configuration. Configure the reverse mapping separately. Missing or empty values disable marking. Save through the bridge editor or configuration and restart the daemon to apply mapping changes.

A crossed ordinary group text message appears as `↔ Dan: hello`. The marker comes before the original sender. The timestamp, sender and body are preserved. Native local repeats are unchanged. Direct messages, signed group subtypes, binary group data and opaque protocol forwarding cannot use this plaintext transformation and remain unchanged. A locally originated message only gets a marker if it subsequently crosses an eligible configured channel mapping.

The marker may contain up to 32 UTF-8 bytes, without colons, control characters or outer whitespace. The sender, marker, separating space and message must fit the firmware's 160-byte text limit. `↔ ` uses four bytes. An over-budget crossed copy is skipped with a metadata-only warning; the original local repeat is preserved. Nothing is truncated.

An exact space-delimited marker already in the sender display is not added again, even behind another bridge's marker. A marker in the message body does not suppress insertion. Original and final packet hashes still pass through the durable echo ledger. A sender name can itself contain the marker, so the label is a display convention, not authenticated proof that a message crossed a bridge.

The offline tests cover UTF-8 boundaries, both directions, channel keys, settings validation, native local repeats, confirmed and failed sends, restart persistence and cycles through independent bridges. These checks do not establish how a particular radio or client renders the glyph.
