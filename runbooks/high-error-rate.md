# High error rate (5xx) on a service

Trigger: 5xx error rate above 5% for more than 5 minutes.

Steps:
1. Check recent logs for stack traces or unhandled exceptions (get_recent_logs).
2. If errors began shortly after a deploy, restart the service to clear the
   faulty process (restart_service). This is the fastest safe first response.
3. If restarting does not help and CPU or memory is saturated, scale out by one
   replica (scale_service).
4. If none of the above applies, do not act — escalate to a human on-call.
