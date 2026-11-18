# Parity

The authoritative 1.0 parity map lives in the harness repo:
[`magitech-harness/PARITY.md`](../magitech-harness/PARITY.md). It is the Phase 8
re-audit deliverable that proves acceptance criterion 1 (every in-scope item
DONE or PARTIAL-with-accepted-gap, nothing MISSING in-scope).

Anima-specific pins at the 1.0 gate:

- Version `0.2.0`, audited at commit `02f728e` on `main`.
- Suite: 1228 tests, 100% statement + branch coverage; CI green on all 6 legs.
- Accepted anima divergences (Malcom, via the ratified conformance map):
  PIN-01 (parallel dispatch raises the first error, not an aggregate) and
  PIN-02 (`Context.provide` notifies at registration). Both are recorded in the
  authoritative map and covered by conformance behavior tests.
- The reference is frozen at `fcc0ad46…` (0.2.2-rc.1) for the 1.0 audit; rc.3
  drift is in the post-1.0 appendix of the authoritative map.
