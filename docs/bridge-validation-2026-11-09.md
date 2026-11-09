# Bridge validation, November 9, 2026

This backend change was tested on macOS with Python 3.12.12, against Repeater base `ebda25f4` and Kesh OpenHop Core `55c44ca`. The legacy MeshCore bridge reference was `5eea466fd2a4fab9c87b5bf22a192d6269fc7777`.

## Original pre-review results

- Broad Repeater suite, excluding the three IPC test files that need local socket access: 3,568 passed, 8 skipped, 3 failed, and 26 subtests passed.
- The three IPC test files with temporary Unix socket access: 16 passed. Their initial sandbox failures were socket permission errors, not protocol failures.
- The three remaining broad-suite failures also fail on an untouched checkout of `ebda25f4` with the same environment. Two `test_mqtt_neighbors.py` fixtures lack the router's `_reply_home_radio` state. `test_proxmox_installer.py::test_readme_documents_new_installer_behavior` still expects upstream installer text in the fork README.
- The strict OpenAPI contract check passes.
- New modules, bridge settings and focused tests pass Ruff checks. Formatting checks pass for new modules and tests. The changed legacy files have 486 existing lint findings against 487 on the base, with no new finding categories/counts. Repository-wide lint is not claimed clean.
- Python compilation and `git diff --check` pass. The patch applies cleanly to the stated Repeater base.

The focused protocol fixtures use the real Core dispatcher/fabric and cryptographic endpoint builders/handlers with fake radios. They cover both RF directions; encrypted login payload preservation; learned and reciprocal PATHs at 1-, 2- and 3-byte widths; opaque requests/responses; TRACE at 1-, 2-, 4- and 8-byte widths; full-key daemon wiring; outbound/return progress; restart echo suppression; malformed legacy direct packets; pause/resume queue invalidation; cancellation after a possible modem write; directional flood budgets; and evidence snapshots taken at actual dispatch.

No live radio transmissions, external test messages, deployment, service restart, or production configuration changes were used for this validation. No claim of RF field reliability or endpoint delivery follows from these simulated tests. New protocol modes and independent observer reception tracking remain opt-in.

## Review-driven revisions

A same-side endpoint case exposed an ambiguity in the initial shared-hop design. Distinct peer routing now uses K for native local paths and a deterministic unsigned V identifier for crossings. New discovery, administration and TRACE modes require that explicit setting. Legacy remains the default; switching modes requires a restart and cross-contact path rediscovery. No dual interpretation of K is attempted.

The revised focused backend set passes 392 tests. It includes 68 cases through actual Core RX parsing and deduplication, PacketRouter and the fake radio fabric; authenticated endpoint login/PATH/admin on both same-side and cross-side routes; K/V TRACE widths 1/2/4/8; marker-loop and short-prefix collision scope; denied crossings without fallback; and the original queue/evidence tests. Independent review reran this set and found no remaining code blocker in the reviewed scope.

Hardening tests exercise real Paho callback dispatch, malformed topic decoding and a healthy report following an exception. Temporary-file ConfigManager tests verify that blocked persistence and lock contention do not block the radio event loop, that accepted control changes survive caller cancellation, and that runtime pause follows durable save. The host-managed observer allowlist is read-only to the settings API.

The revised IPC regression set passes all 16 tests. Changed new modules/tests pass targeted Ruff, changed legacy modules pass F/E9 checks, strict OpenAPI and diff whitespace checks pass. No live radio, MQTT, deployment or configuration changes were used. The full-suite result is recorded separately in the final review report.

## Final Claude follow-ups

The final wording fixes qualify legacy direct transit's crossing-only behavior. The UI now distinguishes a definite rejected or unsaved pause request from an unknown outcome or a lost response. Only an uncertain outcome warns that accepted work may still complete. A successful status refresh remains necessary before retrying uncertain work.

TRACE crossings now reject a requested V hop immediately followed by K or V before queue admission or transmission. Forty-eight added tests exercise actual inbound Core parsing and routing at widths 1/2/4/8, both ingress radios, outbound and return progress, and native K routes with colliding next prefixes. Native K routing and nonconsecutive return crossings remain available. The two `_is_own_echo` call sites only gate neighbor tracking. A regression confirms that saved and active views retain host-managed `reception_evidence`, return a copy, and still reject API writes to that section.

Final checks on this candidate:

- Expanded focused backend set: 537 passed.
- Broad backend suite excluding the three local-socket IPC files: 3,743 passed, 8 skipped, 3 failed, and 26 subtests passed. The failures are the same previously reproduced base failures listed above.
- IPC tests with temporary local Unix-socket access: 16 passed.
- UI: all 524 tests, type checking, and production build passed. Bridge components and tests pass targeted lint. Full UI lint reports 180 existing errors; edited files have the same finding sets as before these follow-ups. A repository-wide lint pass is not claimed.
- Backend changed tests pass Ruff lint and formatting. The changed legacy engine passes F/E9 checks. Strict OpenAPI, Python compilation, and diff whitespace checks pass.

No deployment, restart, live configuration change, commit, push, live RF transmission, or MQTT test publication occurred. The build output is a local artifact only.
