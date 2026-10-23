---
team: payments
service: checkout-service
env: prod
owner: payments-oncall
last_reviewed: 2026-10-02
---

# Scale checkout-service for traffic spikes

## When to use

Use this runbook when checkout-service CPU stays above 75% for more than 10 minutes, or ahead of a planned sales event.

## Prerequisites

- Edit access to the `payments` namespace
- Check the current HPA settings:
  ```
  kubectl get hpa checkout-service -n payments
  ```

## Steps

1. Check current replica count and CPU usage:
   ```
   kubectl top pods -n payments -l app=checkout-service
   ```
2. If the HPA is already at its max, raise the ceiling temporarily:
   ```
   kubectl patch hpa checkout-service -n payments -p '{"spec":{"maxReplicas":20}}'
   ```
3. For an immediate bump, scale manually:
   ```
   kubectl scale deployment/checkout-service -n payments --replicas=12
   ```
4. Watch latency on the checkout dashboard for 10 minutes.
5. Do not exceed 20 replicas without approval from the payments lead. The database connection pool is sized for 20.

## Rollback

After the spike, restore the HPA max to 10 and let it scale down on its own. Do not scale below 3 replicas.

## Escalation

If latency stays high after scaling, the bottleneck is probably the database. Page the payments DBA on-call.
