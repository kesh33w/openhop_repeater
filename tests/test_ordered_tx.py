import asyncio
from types import SimpleNamespace
import pytest
from repeater.ordered_tx import run_ordered_group


def packets():
    return [SimpleNamespace(write_to=lambda: b"identical") for _ in range(2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("bay", "rak"), ("rak", "bay")])
async def test_ordered_completion(order):
    events = []

    async def send(rid, pkt):
        events.append(("start", rid))
        await asyncio.sleep(0.001)
        events.append(("end", rid))
        return True

    rows = await run_ordered_group(order, packets(), send)
    assert events == [
        ("start", order[0]),
        ("end", order[0]),
        ("start", order[1]),
        ("end", order[1]),
    ]
    assert all(row["success"] for row in rows)
    assert rows[0]["raw_packet"] == rows[1]["raw_packet"]


@pytest.mark.asyncio
async def test_known_unsent_continues():
    async def send(rid, pkt):
        return rid == "rak"

    rows = await run_ordered_group(["bay", "rak"], packets(), send)
    assert [r["success"] for r in rows] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "exception", "metadata"])
async def test_uncertain_stops_remaining_leg(failure):
    calls = []

    async def send(rid, pkt):
        calls.append(rid)
        if failure == "exception":
            raise TimeoutError()
        if failure == "metadata":
            pkt._tx_metadata = {"tx_outcome_uncertain": True}
            return False
        return None

    rows = await run_ordered_group(["bay", "rak"], packets(), send)
    assert calls == ["bay"]
    assert rows[0]["metadata"]["tx_outcome_uncertain"]
    assert rows[1]["metadata"]["skipped"]


@pytest.mark.asyncio
async def test_cancel_does_not_start_second():
    calls = []

    async def send(rid, pkt):
        calls.append(rid)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_ordered_group(["rak", "bay"], packets(), send)
    assert calls == ["rak"]
