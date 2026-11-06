"""procurement-ops — Procurement-Ops Pod (LeafMesh).

Boot order matters (SDK 2.4.131):
  1. import agency.tools  → the @global_tool decorators run BEFORE from_store/start,
     so the Sourcing Analyst's read_requisition / check_inventory / find_suppliers /
     estimate_landed_cost / claim_reorder_batch tools are registered.
  2. LeafMesh.from_store() → auto_discover wires agency/*_agent.py to the YAML.
  3. audit_logger.register(...) → attach the WORM sink to the custom external
     procurement_audit_logger_agent (must be after from_store, before start).
  4. leafmesh.start().
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import LeafMesh, LeafMeshLogger

import agency.tools  # noqa: F401 — registers @global_tool tools before start()
from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)

leafmesh = LeafMesh.from_store()

# WORM audit sink — terminus for issued POs, approvals, AND held gates.
_audit_logger.register(leafmesh, "procurement_audit_logger_agent")


async def main():
    await leafmesh.start()

    agents = leafmesh.registry.list_agents() if getattr(leafmesh, "registry", None) else []
    if agents:
        logger.info("Registered agents: " + ", ".join(f"{a.name} ({a.agent_type})" for a in agents))
    logger.info("procurement-ops is running — API docs: http://127.0.0.1:18820/docs")

    # Trigger a requisition (day-0, against the seed store — no connector needed):
    #   curl -X POST http://127.0.0.1:18820/api/mesh/request \
    #     -H "Content-Type: application/json" \
    #     -d '{"entry_point":"new_requisition","data":{"requisition_id":"REQ-DEMO-001","message":"restock for the NA line"}}'

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass

    logger.info("Shutting down...")
    await leafmesh.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
