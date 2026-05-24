"""Per-key correlation confidences.

These weights encode the *strength* of each join key as an identity signal.
They are intentionally explicit constants (not magic numbers in the
correlator) so the rationale stays defensible.

  nhi_id    — IAM-grade identity. If two events share this, they're almost
              certainly the same logical agent. (0.95)
  workload  — Deployment identity. Strong but a workload can host multiple
              identities. (0.90)
  host+pid  — Runtime identity, only meaningful within a tight time window
              because PIDs get recycled by the OS. (0.85, ±5 min)
  repo      — Provenance signal. Multiple agents can ship from one repo, so
              this alone is a weak join. (0.50)
"""

CONF_NHI = 0.95
CONF_WORKLOAD = 0.90
CONF_HOST_PID = 0.85
CONF_REPO = 0.50

HOST_PID_WINDOW_SECONDS = 5 * 60
