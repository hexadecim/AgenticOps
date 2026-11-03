---
id: RB-07
title: Upload storage errors
services: [upload-worker, upload-api]
actions:
  - step: 3
    action: restart_service
    when: the worker holds a stale storage handle after a volume failover
  - step: 4
    action: retry_failed_uploads
    when: uploads stayed in 'received' after the worker recovered
---
# Symptoms
Uploads stay at 0% or time out. Customers report telemetry or log files stuck in the upload screen.
The Upload probe fails with a timeout and 0 bytes stored. upload-worker logs show "storage write failed:
EIO on /vol/uploads" and retries that end with the upload left in 'received'.

# Step 1: confirm the scope
Check the Upload probe for the region and how many sites report the problem. If only one site is affected
and the probe passes, the cause is usually local (network or proxy at the site), not the storage path.

# Step 2: check the storage volume
Look for a recent volume failover or maintenance on /vol/uploads. A failover leaves the worker's open file
handles pointing at the old volume; every write then fails with EIO.

# Step 3: restart the upload worker
Restart upload-worker in the affected region. It is stateless: queued uploads are kept and retried
automatically once it is back (about one minute). Re-run the Upload probe afterwards.

# Step 4: retry uploads that stayed in 'received'
If uploads are still in 'received' after the worker recovered, queue them again for the affected site.

# Escalation
If the probe still fails after the restart, page storage on-call; do not restart again.
