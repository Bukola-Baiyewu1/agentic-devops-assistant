# CPU or memory saturation and high latency

Trigger: CPU above 85% or p95 latency above target for 10 minutes.

## Confirm the load is real

Check current CPU, replicas, and error rate with get_service_health to make
sure the saturation is not a metrics glitch.

## Scale out by one replica

If CPU is saturated or latency is high because of load, scale out by exactly
one replica with scale_service to shed load. Never add more than one replica
per approval and never exceed the configured replica limit.

## Escalate if saturation persists

If saturation or high latency persists after scaling, escalate to a human.
