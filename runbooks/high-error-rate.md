# High error rate (5xx) on a service

Trigger: 5xx error rate above 5% for more than 5 minutes.

## Check recent logs first

Read the recent logs for stack traces, 500 errors, or unhandled exceptions with
get_recent_logs. Reading logs is safe and needs no approval.

## Restart after a recent deploy

If 5xx errors or unhandled exceptions began shortly after a deploy, restart the
service to clear the faulty process with restart_service. This is the fastest
safe first response to a bad process state.

## Scale out if the service is also saturated

If a restart did not help and CPU or memory is saturated, scale out by exactly
one replica with scale_service.

## Otherwise escalate

If none of the above applies, do not act. Escalate to the human on-call
engineer with the logs you gathered.
