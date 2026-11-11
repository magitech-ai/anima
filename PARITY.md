# Baseline pins (0.2.0, Phase 0b, 2026-11-11)

- Version: `0.2.0` (first minor bump after 0.1.0; the Phase 0a re-sync ships here).
- Verified suites against this ref (Python 3.12, sibling-layout clones):
  anima 538 passed (100% coverage), magitech-harness 118/118 (100%),
  mth-plugins 260/260 (100%).
- The Phase 0a reflect fix is in main (merge commit `fb3b9c1`):
  the reflect service's special-property branches use
  `object.__getattribute__` / `object.__setattr__` so a missing
  reserved-word attribute raises `AttributeError` instead of recursing.
- Sibling pins: magitech-harness declares `anima>=0.1,<0.2`;
  mth-plugins declares the same range as a direct dependency.
