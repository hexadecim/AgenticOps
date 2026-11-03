---
id: RB-03
title: Login and access failures
services: [login, portal]
actions:
  - step: 2
    action: restart_service
    when: the login service stopped answering after a certificate or configuration reload
---
# Symptoms
Users cannot sign in to the portal, see "invalid session" after entering a correct password, or the login
page does not load. The Login probe fails.

# Step 1: rule out a single account
If one user is affected and the Login probe passes, it is an account problem (locked, expired password, MFA
reset needed). Handle it in the identity tool; nothing to restart.

# Step 2: restart the login service
When the Login probe fails for everyone, restart login in the region. Sessions are kept in the shared store,
so signed-in users stay signed in.

# Escalation
If restarts do not help, check the identity provider status page and page identity on-call.
