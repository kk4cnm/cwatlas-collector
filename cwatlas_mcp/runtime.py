"""Wires the pieces together and runs the supervisor + MCP sidecar in one asyncio app.

Model B: the supervisor is the long-lived core and keeps capturing even if the MCP
interface (or the agent) goes away. MCP is attached as a view over shared state.

This is a skeleton entrypoint — search workers and capture workers are stubbed; fill
them in at milestones M0..M2 (see ../DESIGN.md).
"""
from __future__ import annotations

import asyncio

from . import server
from .scheduler import CollectorState, ControlBus, SchedulerConfig, Supervisor
from .sdr_client import SdrClient, SdrConfig

# CW watering holes to search (band -> center_hz for the waterfall view). [verify spans]
SEARCH_PLAN = {
    "80m": 3_550_000,
    "40m": 7_020_000,
    "30m": 10_125_000,
    "20m": 14_035_000,
    "17m": 18_081_000,
    "15m": 21_035_000,
    "12m": 24_910_000,
    "10m": 28_035_000,
    "6m": 50_050_000,
}


async def search_worker(sdr: SdrClient, sup: Supervisor, band: str, center_hz: float):
    """Consume one /W/F stream, run the CW detector, feed detections to the supervisor."""
    async for _frame in sdr.waterfall_stream(center_hz, zoom=10, fps=3):
        # TODO[M0]: per-bin energy vs rolling noise floor + keying-envelope signature
        #           -> emit Detection(freq_hz, band, strength_db, keyed_confidence).
        #           sup.observe(det)
        pass


async def ptt_worker(sup: Supervisor):
    """Read operator TX state (Flex amp-key via GPIO/serial or SmartSDR CAT) -> sup.set_tx()."""
    # TODO[M3]: real PTT ingest. Hardware interlock is separate and NOT here.
    while True:
        await asyncio.sleep(0.1)


async def main():
    sdr = SdrClient(SdrConfig())
    state = CollectorState()
    bus = ControlBus()
    sup = Supervisor(SchedulerConfig(), state, bus)

    server.attach(state, bus, sdr)

    tasks = [
        asyncio.create_task(sup.run()),
        asyncio.create_task(ptt_worker(sup)),
    ]
    for band, cf in SEARCH_PLAN.items():
        tasks.append(asyncio.create_task(search_worker(sdr, sup, band, cf)))

    # Run the MCP server (stdio) alongside the supervisor. For a remote agent, swap to
    # streamable-HTTP: mcp.run(transport="streamable-http").
    tasks.append(asyncio.create_task(server.mcp.run_stdio_async()))

    try:
        await asyncio.gather(*tasks)
    finally:
        await sdr.aclose()


if __name__ == "__main__":
    asyncio.run(main())
