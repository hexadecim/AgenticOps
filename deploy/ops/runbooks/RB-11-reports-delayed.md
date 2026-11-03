---
id: RB-11
title: Reports and analytics delayed
services: [analytics, reports]
actions:
  - step: 2
    action: restart_service
    when: the analytics scheduler is stuck and the last job is older than one hour
---
# Symptoms
Daily reports are missing or show old numbers. The Analytics probe reports that the last job is older than
one hour.

# Step 1: check the job queue
A long-running export can hold the queue. Wait for it if it is still progressing.

# Step 2: restart analytics
If the scheduler is stuck, restart analytics in the region; the next job starts within five minutes.
