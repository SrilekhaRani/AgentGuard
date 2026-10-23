---
team: payments
service: payments-db
env: prod
owner: payments-oncall
last_reviewed: 2026-08-30
---

# Rotate payments database credentials

## When to use

Use this runbook for the scheduled 90-day credential rotation, or immediately if a credential may have been exposed (for example, pasted into a ticket or chat).

## Prerequisites

- Access to the secrets manager path `payments/prod/db`
- A maintenance window announced in #payments-incidents at least 1 hour ahead
- The current connection string, which looks like this:
  ```
  DATABASE_URL=postgres://payments_app:FAKE-db-pass-123@payments-db.internal:5432/payments
  ```

## Steps

1. Create a new database user with the same grants as `payments_app`.
2. Store the new password in the secrets manager under `payments/prod/db/next`.
3. Update the payments-api deployment to read from the new secret path.
4. Trigger a rolling restart (see [[restart-payments-api]]).
5. Confirm all pods are connected with the new user:
   ```
   SELECT usename, count(*) FROM pg_stat_activity GROUP BY usename;
   ```
6. Once no sessions use the old user, revoke its login.

## Rollback

If pods fail to connect, point the deployment back to the old secret path and restart. Do not drop the old user until the new one has run cleanly for 24 hours.

## Escalation

If connections fail for more than 5 minutes, page the payments DBA on-call.
