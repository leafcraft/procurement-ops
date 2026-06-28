"""procurement-ops — LeafMesh Multi-Agent Project

Agent registration:
  - agency/*_agent.py  → auto_discover matches function names to YAML agent names

Standalone decorators (no leafmesh instance needed):
  from leafmesh import pre_compose, chain, compose, conditional_chain, chain_with_results
"""
import asyncio
import signal
from dotenv import load_dotenv
from leafmesh import LeafMesh, LeafMeshLogger

from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)

# auto_discover in config.yaml wires agency/*_agent.py files automatically
leafmesh = LeafMesh.from_yaml("configs/config.yaml")

# Register the hash-chained WORM audit sink as the intelligence function
# for the custom external procurement_audit_logger_agent (SDK 2.4+ API).
# Must happen after from_yaml() and before start(). This sink is the
# terminus for issued POs AND for approval gates held on timeout
# (default-deny) — a held financial commitment is recorded, never dropped.
_audit_logger.register(leafmesh, "procurement_audit_logger_agent")


async def main():
    await leafmesh.start()

    # leafmesh.start() launches a built-in API server at http://127.0.0.1:18820
    # See config.yaml for the full endpoint reference, or visit /docs for interactive API docs.
    # Key endpoints:
    #   POST /api/mesh/request            — trigger agent workflows via entry points
    #   POST /api/mesh/stream             — SSE stream of LLM response
    #   POST /webhook/{entry_point}       — unified webhook (new task or human response)
    #   GET  /docs                        — interactive API docs (ReDoc)

    # Log registered agents
    agents = leafmesh.registry.list_agents() if hasattr(leafmesh, 'registry') and leafmesh.registry else []
    if agents:
        agent_summary = ", ".join(f"{a.name} ({a.agent_type})" for a in agents)
        logger.info(f"Registered agents: {agent_summary}")
    logger.info("procurement-ops is running — press Ctrl+C to stop")
    logger.info("API docs: http://127.0.0.1:18820/docs")
    logger.info("Docker:   docker compose up --build")

    # ─── mesh_call: the external entry point into the agent mesh ───
    # This is how you trigger agent workflows from code, APIs, or webhooks.
    # The mesh automatically chains agents via can_call rules in config.yaml.
    #
    # result = await leafmesh.mesh_call(
    #     "greet_user",
    #     {"message": "Hello, I need help organizing my tasks"},
    #     session_id="optional-session-id"
    # )
    # logger.info(f"Result: {result}")
    #
    # # Direct research (bypasses greeting flow):
    # result = await leafmesh.mesh_call("research", {"research_topic": "market trends"})
    #
    # # Direct advice:
    # result = await leafmesh.mesh_call("advise", {"processed_data": {...}})
    #
    # # On-demand scheduled report (also runs daily at 9 AM UTC via cron):
    # result = await leafmesh.mesh_call("scheduled_report", {"trigger": "manual"})

    # ─── Usage analytics & LLM cache stats ───
    # These are available after agents have run:
    #
    # analytics = await leafmesh.get_usage_analytics()
    # logger.info(f"Usage: {analytics}")
    #   → Returns: agent call counts, error rates, avg latency per agent
    #
    # cache_stats = await leafmesh.get_llm_cache_stats()
    # logger.info(f"LLM cache: {cache_stats}")
    #   → Returns: hit_count, miss_count, hit_rate, cached_keys
    #
    # ─── Upstream yields ───
    # Agents define yields in YAML (typed key-value pairs).
    # The SDK auto-stores them to Redis and auto-injects them into
    # downstream agents as input_data["upstream_yields"][agent_name].
    # Fan-in agents receive yields from ALL contributing upstream agents.

    # Keep the process alive until interrupted
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    import sys
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        # Windows: add_signal_handler not supported, use KeyboardInterrupt
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
