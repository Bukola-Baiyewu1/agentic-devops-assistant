"""Central configuration. Everything is overridable via environment variables."""
import os

# Where the JSON state file lives (events, actions, traces).
STATE_PATH = os.getenv("STATE_PATH", ".aegis_state.json")

# Folder containing your runbooks (the RAG corpus).
RUNBOOKS_DIR = os.getenv("RUNBOOKS_DIR", "runbooks")

# Anthropic settings. If ANTHROPIC_API_KEY is unset, the agent falls back to a
# deterministic mock planner so the whole flow still runs end to end.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Set this to whatever Claude model you have access to (e.g. claude-sonnet-5).
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-5")

# Safety cap so the planning loop can never run forever.
MAX_PLAN_ITERATIONS = int(os.getenv("MAX_PLAN_ITERATIONS", "3"))


def use_real_llm() -> bool:
    return bool(ANTHROPIC_API_KEY)
