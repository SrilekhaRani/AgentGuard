---
team: platform
service: job-queue
env: prod
owner: platform-oncall
last_reviewed: 2026-07-11
---

# Clear a stuck job queue

## When to use

Use this runbook when the job-queue backlog grows for more than 20 minutes while workers report as idle. That usually means a poison message is blocking the head of the queue.

## Prerequisites

- Access to the queue admin CLI
- Confirm the workers are actually idle, not just slow:
  ```
  kubectl logs -n platform -l app=queue-worker --since=10m | tail -50
  ```

## Steps

1. Inspect the first message in the queue:
   ```
   queuectl peek --queue jobs-default --count 1
   ```
2. If the message is malformed or keeps failing, move it to the dead-letter queue:
   ```
   queuectl move --queue jobs-default --to jobs-dlq --count 1
   ```
3. Watch whether workers start consuming again within 2 minutes.
4. Repeat steps 1 to 3 up to 5 times. If more than 5 messages are bad, stop and escalate. Something upstream is producing bad jobs.
5. Never purge the whole queue. Purging deletes customer jobs permanently.

## Rollback

Messages in the dead-letter queue can be replayed with:
```
queuectl move --queue jobs-dlq --to jobs-default --count 1
```

## Escalation

If the queue is still stuck after removing 5 messages, page the platform secondary on-call and notify the team that owns the producing service.
