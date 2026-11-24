# 0022. Remove the restricted expression dialect

Date: 2026-11-24
Status: accepted (user decision, independently reviewed)

## Context

ADR 0006 ported a restricted JavaScript expression evaluator because the
rc.13 conformance target evaluated `!!js` config expressions with a full JS
engine. The conformance milestone is complete and ADR 0017 makes Animas
independent: no upstream release is a target, and `PARITY.md` is a historical
record rather than a roadmap.

Evidence gathered in 2026-11-24:

- The only authored `!!js` expressions in any Magitech repo were two rows in
  `magitech-harness/profiles/base.yaml` (platform gating for the bash/pwsh
  executors).
- GitHub code search found `from animas import Context` only in Magitech repos;
  0 stars, 1 stale fork, 4 contributors; PyPI 1.0.0–1.0.2 published the same
  day; no download statistics exist. External use was not proven.
- The maintainer reported that they use opencode and DeepSeek Harness as
  agents, not the Animas stack, and consider breaking changes acceptable at
  this stage.
- The evaluator was contained (allow-listed, 100% branch coverage, no
  dependencies) but ~750 lines plus ~830 lines of tests served those two rows.
- Mature Python config systems are declarative-first, treating general
  expressions as an opt-in escape, not a default.

`docs/COMPATIBILITY.md` requires a deprecation release before a major removal.
On the evidence above the user chose to skip that cycle deliberately: the
policy is amended for this pre-adoption major release rather than silently
ignored. If Animas later gains real external consumers, the general
deprecation policy applies again.

## Decision

- Remove the restricted expression evaluator (`src/animas/jsexpr.py`) and its
  public exports (`JsExprError`, `evaluate`, `interpolate`).
- Remove the `!!js` YAML tag and `__jsExpr` config dialect. `profile_yaml.py`
  remains as plain PyYAML load/dump plus config-path line/column locations.
- Replace the two platform-gating expressions with declarative entry options
  `disabled_on` / `enabled_on`, backed by a neutral `platform_name()` helper
  in `core.py` (independent of any evaluator).
- Remove the `internal/config` interpolation hook and interpolation of
  `inject` and `intercept` values. Config values are literal data.
- Supersede ADR 0006. Its historical decision and upstream citations remain
  legible in `PARITY.md` (two expression rows marked "DONE (historical)").
- Bump Animas to `2.0.0`; downstream constraints move to `animas>=2,<3`.

## Consequences

- The public API shrinks: `JsExprError`, `evaluate` and `interpolate` are gone.
- Profiles are declarative; platform conditions use `disabled_on`/`enabled_on`.
- Config expressions from older profiles no longer load; they fail at YAML
  parse time with a clear PyYAML constructor error rather than being silently
  ignored.
- The kernel loses a general expression capability; reintroducing one would be
  a new ADR with evidence of real need.
- No deprecation cycle was observed for this pre-adoption major release; this
  is a deliberate exception to the general policy in `COMPATIBILITY.md`, not
  a repeal of it.
