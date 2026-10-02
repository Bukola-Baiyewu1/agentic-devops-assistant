# Database connection pool exhausted

Trigger: the application reports that the database connection pool is
exhausted, or connection timeouts to the database.

## Do not restart the database

Database restarts, failovers, and configuration changes are never automated.

## Escalate to the database owner

Escalate connection pool exhaustion and database timeouts to the database
owner. Include the error messages from the recent logs.
