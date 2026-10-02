# CPU or memory saturation / high latency

Trigger: CPU above 85% or p95 latency above target for 10 minutes.

Steps:
1. Confirm the load is real and not a metrics glitch (get_service_health).
2. Scale out by one replica to shed load (scale_service).
3. If saturation persists after scaling, escalate to a human.
