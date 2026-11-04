"""Load tests run on the test infrastructure only (make test-infra-up): ports 55432, 56379, 53025, 53143."""

from __future__ import annotations

POSTGRES = "localhost:55432/opspilot"
REDIS = "redis://localhost:56379/2"
