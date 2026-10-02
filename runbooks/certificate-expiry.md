# TLS certificate expiry

Trigger: a TLS certificate expires in fewer than 14 days, or has expired.

## Escalate certificate renewal

Certificate renewal changes security configuration and must be performed by a
human. Do not restart or scale the service for a certificate alert. Escalate
to the platform team with the certificate subject and expiry date.
