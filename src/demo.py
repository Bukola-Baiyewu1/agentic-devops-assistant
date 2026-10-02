"""A fake target service the agent monitors and fixes.

It is bundled into the app so the whole demo runs with one command. In a real
deployment this stands in for a service in your cluster, and the action tools
would call the Docker/Kubernetes API instead of this simulator.
"""
import time


class ServiceSimulator:
    def __init__(self, name: str = "web"):
        self.name = name
        self.reset()

    def reset(self):
        self.broken = False
        self.replicas = 1
        self.cpu = 30  # percent
        self._logs = ["[info] service started", "[info] listening on :8000"]

    # ---- fault injection (used by the /demo/* endpoints) -------------
    def inject_error(self):
        self.broken = True
        self.cpu = 92
        self._log("[error] 500 Internal Server Error (x37)")
        self._log("[error] unhandled exception in request handler")

    def clear_error(self):
        self.broken = False
        self.cpu = 30
        self._log("[info] service recovered, error rate back to normal")

    def _log(self, line: str):
        self._logs.append(line)
        self._logs = self._logs[-200:]

    # ---- read surface ------------------------------------------------
    def health(self) -> dict:
        return {
            "service": self.name,
            "status": "unhealthy" if self.broken else "healthy",
            "error_rate_pct": 12.5 if self.broken else 0.1,
            "cpu_pct": self.cpu,
            "replicas": self.replicas,
        }

    def logs(self, lines: int = 100) -> str:
        return "\n".join(self._logs[-lines:])

    # ---- action surface (called by the approved tools) ---------------
    def snapshot(self) -> dict:
        """Capture current state so an action can be rolled back."""
        return {"broken": self.broken, "replicas": self.replicas, "cpu": self.cpu}

    def restore(self, snap: dict):
        self.broken = snap["broken"]
        self.replicas = snap["replicas"]
        self.cpu = snap["cpu"]
        self._log(f"[info] rolled back to previous state: {snap}")

    def restart(self):
        self._log("[info] restart requested — clearing faulty process")
        time.sleep(0.05)
        self.clear_error()

    def scale(self, replicas: int):
        self.replicas = replicas
        self.cpu = max(10, self.cpu - 30)
        self._log(f"[info] scaled to {replicas} replicas")


sim = ServiceSimulator()
