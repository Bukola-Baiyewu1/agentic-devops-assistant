# Disk pressure on a host

Trigger: disk usage above 85% on a host or volume.

## Never automate disk cleanup

Disk pressure is never auto-remediated. Deleting data is never automated
because it cannot be undone.

## Escalate disk pressure to a human

Always escalate disk usage alerts to the human on-call engineer with the
current disk usage and the largest directories.
