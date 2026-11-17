"""Hardware-independent ordered physical TX result collection."""
import asyncio


async def run_ordered_group(targets, packets, send_leg, *, results=None):
    """Run each physical leg once in configured order; abort on uncertainty.

    A definitely-unsent failure allows the next leg. Cancellation is propagated
    and never starts a later leg. Results distinguish skipped from failed sends.
    """
    results = [] if results is None else results
    uncertain = False
    stop_reason = None
    for radio_id, packet in zip(targets, packets):
        if stop_reason:
            metadata = {"ok": False, "reason": stop_reason,
                        "skipped": True, "tx_outcome_uncertain": False}
            success = False
        else:
            try:
                outcome = await send_leg(radio_id, packet)
                metadata = dict(getattr(packet, "_tx_metadata", None) or {})
                uncertain = bool(metadata.get("tx_outcome_uncertain")) or (outcome is not True and outcome is not False)
                success = outcome is True and not uncertain and not metadata.get("suppressed")
            except asyncio.CancelledError:
                results.append({
                    "radio_id": radio_id, "success": False,
                    "metadata": {"reason": "cancelled_during_leg", "tx_outcome_uncertain": True},
                    "raw_packet": packet.write_to().hex(), "failure_reason": "Cancelled; TX uncertain",
                })
                for remaining_id, remaining_packet in list(zip(targets, packets))[len(results):]:
                    results.append({
                        "radio_id": remaining_id, "success": False,
                        "metadata": {"reason": "group_cancelled", "skipped": True},
                        "raw_packet": remaining_packet.write_to().hex(), "failure_reason": "Group cancelled",
                    })
                raise
            except Exception as exc:
                metadata = dict(getattr(packet, "_tx_metadata", None) or {})
                metadata.update(reason=f"TX exception: {type(exc).__name__}", tx_outcome_uncertain=True)
                uncertain, success = True, False
            if uncertain:
                metadata["tx_outcome_uncertain"] = True
                stop_reason = "previous_leg_uncertain"
            elif metadata.get("suppressed"):
                stop_reason = "previous_leg_suppressed"
            elif metadata.get("reason") == "destination cache full":
                stop_reason = "destination_cache_full"
        results.append({
            "radio_id": radio_id, "success": success, "metadata": metadata,
            "raw_packet": packet.write_to().hex(),
            "failure_reason": None if success else metadata.get("reason", "TX not confirmed"),
        })
    return results
