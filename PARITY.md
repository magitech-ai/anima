# Parity

The authoritative 1.0 parity map lives in the harness repo:
[`magitech-harness/PARITY.md`](../magitech-harness/PARITY.md). It is the Phase 8
re-audit deliverable that proves acceptance criterion 1 (every in-scope item
DONE or PARTIAL-with-accepted-gap, nothing MISSING in-scope).

Anima-specific pins at the 1.0 gate:

- Version `0.3.0`, audited at commit `02f728e` on `main`.
- Suite: 1228 tests, 100% statement + branch coverage; CI green on all 6 legs.
- Accepted anima divergences (Malcom, via the ratified conformance map):
  PIN-02 (`Context.provide` notifies at registration) remains PARTIAL. PIN-01
  (parallel dispatch failure aggregation) was **re-ratified from PARTIAL to
  DONE on 2026-11-19** at Malcom's decision: parallel dispatch now aggregates
  failures (one → original error, ≥2 → `ExceptionGroup`), with a conformance
  probe in `tests/integration/test_phase1_acceptance.py`. Both states are
  recorded in the authoritative map.
- The reference is frozen at `fcc0ad46…` (0.2.2-rc.1) for the 1.0 audit; rc.3
  drift is in the post-1.0 appendix of the authoritative map.
