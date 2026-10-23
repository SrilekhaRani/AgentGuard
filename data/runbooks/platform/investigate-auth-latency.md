---
team: platform
service: auth-service
env: prod
owner: platform-oncall
last_reviewed: 2026-09-21
---

# Investigate high latency on auth-service

## When to use

Use this runbook when auth-service p99 latency is above 800ms for more than 5 minutes. Slow auth affects every other service, so treat it as urgent.

## Prerequisites

- Read access to the `platform` namespace and the auth-service dashboard
- Read access to auth-service logs

## Steps

1. Check whether the slowdown is in auth-service itself or a dependency:
   ```
   kubectl logs -n platform -l app=auth-service --since=15m | grep "upstream_ms"
   ```
2. If the token cache hit rate is below 80%, check the Redis cluster:
   ```
   redis-cli -h auth-cache.internal INFO stats
   ```
3. If calls to the identity provider are slow, check the provider's status page and the outbound proxy.
4. For local debugging, export the service token first. The example value in older docs was:
   ```
   export AUTH_SERVICE_TOKEN=ghp_FAKEexampleTOKEN000000000000000000000
   ```
   Never use a real token in a shared terminal.
5. If one pod is slow and the others are fine, delete only that pod and let the deployment replace it.

## Rollback

This runbook is read-only except for step 5. A deleted pod is replaced automatically.

## Escalation

If latency affects login for more than 10 minutes, open a SEV-1 and page the platform lead.
