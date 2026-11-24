# 0018. Remove the Anima-original CLI/server/HTTP surfaces

Date: 2026-11-24
Status: accepted (user decision)

## Context

The modules `server`, `server_acl`, `server_echo`, `server_proxy`,
`server_static`, `http`, `http_socks`, `cli`, `cli_create`, and `cli_env` were
Magitech-original additions, explicitly marked `†` in the README and never
claimed as part of the Cordis-derived kernel. A direct grep across
`magitech-harness`, `mth-plugins`, `home-swe-home`, and `agents-home` found no
imports of any of them; only Anima's own tests used them. Their sole product
role was a standalone `anima run` / `anima create` CLI and a demo HTTP/WebSocket
stack (~3,449 source lines plus 15 dedicated test files).

With Anima reaching 1.0, keeping them would freeze a compatibility promise
around surface that no consumer uses and that is not part of the paradigm.

## Decision

- Delete those modules, their exports, the `anima` console script, and their
  tests before the 1.0.0 release.
- Keep `anima/utils.py`: it is used by `schema.py` and was never marked `†`.
- Host applications (for example `magitech-harness`) own their own CLI, HTTP,
  and server surfaces on top of the kernel.
- ADR 0016's statement that the `†` modules were unchanged described that
  decision at the time; it is superseded by this ADR.

## Consequences

- `anima` is a library kernel with no published CLI; `load_profile(ctx, path)`
  is the supported way to load a declarative profile.
- The 1.0 compatibility policy covers only the kernel surface listed in
  `docs/COMPATIBILITY.md`.
- No downstream Magitech repo required a code change: none imported these
  modules.
