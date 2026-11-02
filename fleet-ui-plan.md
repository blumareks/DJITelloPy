# Fleet UI plan

## Overview

Build an independent Streamlit web application (`fleet_ui/`) that controls several Tello REST services
(the `tello_api` service, one per drone) at once. The user defines drones (IP on the 192.168.21.x
Ethernet + API key), writes a step script (one instruction per row, mirroring the REST API, plus
`sleep N`), and uses **Execute**, **Abort** and **Land all**. Every call is logged as
`drone N, ip, command, response`, to the process console and to a panel in the page.

The fleet UI does **not** import `djitellopy` or `tello_api`. It talks to the services only over HTTP(S).

To make Abort and Land all useful while a drone is mid-movement, the existing REST API needs two small
changes first: a new `POST /v1/abort` and a `POST /v1/land` that can interrupt a running command.

### Decisions already made with the user
- Abort = hover all drones (Tello `stop`). Land all = land all drones.
- API is extended: `POST /v1/abort`, and `POST /v1/land` preempts a running command.
- No warning banner in the UI.

### Security stance (project rule, not overridable)
`.bob` security rules require TLS 1.2+ with certificate validation for all network traffic and forbid
sending API keys unencrypted. The user asked for plain HTTP; the plan therefore uses **HTTPS only**, with a
helper script that creates a private CA and one certificate per drone IP, so the lab setup stays small.
Certificate validation is never disabled. If the user wants a different compliant transport, only
sub-tasks 3, 4 and 5 change.
Other security decisions:  API keys only in the browser session
(never written to disk, never logged), Streamlit bound to 127.0.0.1 by default, usage statistics off.

### Assumptions (to confirm)
- Steps run in lockstep: step N is sent to all drones at the same time and the next step starts when all
  have answered (REST calls block until the movement is done). `sleep N` pauses the whole fleet.
- If a drone fails a step, the run halts (no further steps) and nothing is sent automatically; the
  operator decides (Abort / Land all).
- "Console" = the terminal running Streamlit, mirrored in an in-page log panel.
- Step script is a table with one editable text cell per row.

### Step language (mirrors the REST API)
`takeoff`, `land`, `up|down|left|right <cm 20-500>`, `turn left|right <degrees 1-360>`,
`record on|off`, `sleep <seconds>`. Blank lines and `#` comments are ignored. The whole script is validated
before anything is sent; errors are reported with row numbers.

## Sub-tasks

### 1. API: `POST /v1/abort` and preemptive `POST /v1/land`
- **Intent**: let an operator stop or land a drone while a movement is running. Today any second command
  gets `409` because the controller lock is held and the library waits for the drone's reply.
- **Expected outcomes**:
  - `POST /v1/abort` returns 200 immediately and makes the drone hover, even mid-movement.
  - `POST /v1/land` lands the drone even if another command is running.
  - An interrupted command ends cleanly (documented status) and is **not retried** by the library, which
    would otherwise re-send the interrupted movement after an abort.
  - A stray reply from `stop`/`land` is not mistaken for the answer to a later command.
  - Both endpoints appear in the OpenAPI spec and Swagger UI; committed `openapi.json` regenerated.
- **Todo List**:
  1. Add an abort signal to the controller and make the Tello wrapper refuse to (re)send commands while it
     is set; clear it when the next command takes the lock.
  2. Send `stop` / `land` without waiting for a reply when the lock is busy (normal path when idle).
  3. Drop stale replies before each new command.
  4. Add the abort endpoint and update the land endpoint, with tags/descriptions/error responses in the
     same style as existing endpoints.
  5. Tests with the fake drone (same pattern as the existing blocked-command test): abort mid-move, land
     mid-move, no re-send after abort, next command works afterwards, auth required.
  6. Regenerate `tello_api/docs/openapi.json`; update the README endpoint table.
- **Relevant Context**: `tello_api/drone.py` (`DroneController._session`, `_lock`, `land`),
  `tello_api/app.py` (router, `ERROR_RESPONSES`), `djitellopy/tello.py` (`send_command_with_return` polling
  loop at ~437, `send_control_command` retry loop at ~486, `send_command_without_return` at ~477,
  `emergency`), `tests/test_api.py` (`FakeTello.block`, `test_concurrent_command_gets_409`),
  `tello_api/export_openapi.py`.
- **Risk to flag**: behaviour of a real Tello when `stop` arrives during a movement (which reply it sends for
  the interrupted command) is not verified in this environment; needs a check on hardware.
- **Status**: `[x] done`
- **Context for subsequent sub-tasks**:
  - `POST /v1/abort`:
    - Returns `200` immediately with body `{"status": "ok", "command": "abort"}`.
    - Non-blocking: sets internal abort signal and sends Tello `stop` without waiting for return or acquiring the busy lock.
    - Requires `X-API-Key` header (`401` if missing/invalid).
  - `POST /v1/land`:
    - Returns `200` with body `{"status": "ok", "command": "land"}`.
    - Preempts any currently running movement by setting the abort signal and sending Tello `land` without waiting if the lock is held; runs normal synchronous land if idle.
    - Requires `X-API-Key` header (`401` if missing/invalid).
  - Interrupted commands:
    - An in-flight flight command (such as `POST /v1/move/*` or `POST /v1/turn/*`) interrupted by `abort` or preemptive `land` ends cleanly with HTTP `502` (`{"detail": "Drone command failed"}`) and will **not** be retried.
    - The controller drains any stale responses from the UDP buffer prior to executing subsequent commands.
  - Review follow-up (found and fixed after the sub-task): a leftover abort flag blocked the keepalive loop, so a
    drone left hovering after `abort` would have auto-landed after 15 s. The keepalive now clears the flag when it
    holds the lock. Regression tests in `tests/test_abort_real_tello.py` use the real `Tello` class with a fake UDP
    socket (43 tests pass in total).
  - For the fleet UI: after `abort` a drone keeps hovering until told otherwise. `abort` before the service ever
    talked to the drone does nothing but still returns `200`. A movement interrupted by abort/land returns `502` to
    the original caller, so the run engine must treat `502` from a step that was interrupted by its own Abort/Land
    as expected, not as a new failure.
  - Not verified on hardware: which reply a real Tello sends for an interrupted movement.

### 2. Fleet UI: step parser
- **Intent**: turn the script text into validated steps, independent of Streamlit and of the network.
- **Expected outcomes**: a pure module that returns a list of steps or a list of errors with row numbers;
  ranges match the REST API (20-500 cm, 1-360 degrees); sleep must be positive and capped.
- **Todo List**:
  1. Create `fleet_ui/` package with requirements file (Streamlit and HTTP client, latest stable, pinned).
  2. Implement the parser for the step language above (case-insensitive, extra whitespace tolerated,
     comments/blank rows skipped).
  3. Unit tests: every valid instruction, every error case, row numbering, empty script.
- **Relevant Context**: `tello_api/app.py` (`MoveRequest`, `TurnRequest` limits; route shapes),
  `tests/` layout.
- **Status**: `[ ] pending`

### 3. Fleet UI: API client and run engine
- **Intent**: execute a validated script against all drones, with abort and land-all, independent of the UI.
- **Expected outcomes**:
  - Drone config is validated (IPv4 inside 192.168.21.0/24, port, non-empty key).
  - Each step goes to all drones concurrently; next step starts when all answered; `sleep` waits and can be
    interrupted by Abort.
  - Failure on any drone halts the run; Abort/Land all work during a run and when idle.
  - Every call is logged as `drone N, ip, command, response` (key never included) to the console logger and
    to an in-memory log the UI can read.
  - HTTPS with certificate validation, optional CA bundle path, explicit timeouts (short for abort/land, long
    enough for takeoff/moves).
  - Runner runs in a background thread so Streamlit reruns do not interrupt it.
- **Todo List**:
  1. HTTP client mapping each step to the REST call (`takeoff`, `land`, `move/{dir}`, `turn/{dir}`,
     `recording`, plus `abort`).
  2. Run engine: per-step fan-out/barrier, stop event, run snapshot of drones+script taken at Execute.
  3. Logging (structured line format, console + in-memory buffer, no secrets).
  4. Tests against a small fake HTTP(S) server: lockstep order, sleep timing, abort during sleep and during
     a request, land all, failure halts run, key never in logs, TLS verification on.
- **Relevant Context**: REST contract in `tello_api/app.py` and `tello_api/docs/openapi.json`; error codes
  200/401/409/422/502.
- **Status**: `[ ] pending`

### 4. Fleet UI: Streamlit page
- **Intent**: the operator-facing app, `fleet_ui/fleet_ui.py`.
- **Expected outcomes**:
  - Add/remove drone rows: IP, port, API key (masked input). Rows numbered as "drone 1, drone 2, ...".
  - Editable step table (add/delete/reorder rows by editing), with validation errors shown next to the rows.
  - Buttons: **Execute**, **Abort** (hover all), **Land all**; Execute disabled while running; after finish or
    abort, everything is editable again and Execute restarts from step 1.
  - Live log panel (auto-refresh) plus the same lines in the terminal; clear-log button; current step and
    per-drone status shown.
  - Keys held only in session state; nothing persisted.
- **Todo List**:
  1. Page layout and session-state handling; snapshot inputs when Execute is clicked.
  2. Wire buttons to the run engine; auto-refreshing log/status area.
  3. `.streamlit/config.toml` in `fleet_ui/`: bind 127.0.0.1, usage stats off.
  4. Streamlit `AppTest` smoke test: add drones, invalid script shows errors, Execute/Abort/Land wiring
     against the fake server.
- **Relevant Context**: sub-tasks 2 and 3 modules; Streamlit reruns the script on each interaction, so run
  state must live outside the script (shared engine object).
- **Status**: `[ ] pending`

### 5. TLS helper and run instructions
- **Intent**: make HTTPS practical in the lab so keys are never sent in clear text.
- **Expected outcomes**: one script creates a private CA and a certificate per drone IP (IP in the
  certificate's subject alternative names); instructions show how to start each `tello_api` service with
  `TLS_CERTFILE`/`TLS_KEYFILE` and how to point the UI at the CA bundle.
- **Todo List**:
  1. Helper script (standard `openssl`, no new Python dependency), private keys created with restrictive
     permissions, output kept out of git (`.gitignore`).
  2. Document the flow: generate, copy cert+key to each Pi, start service, set CA bundle in the UI.
  3. Check that the UI rejects a service whose certificate is not signed by the CA.
- **Relevant Context**: `tello_api/__main__.py` (TLS env vars already supported), `README.md` REST API
  section.
- **Status**: `[ ] pending`

### 6. Documentation, dependencies, final validation
- **Intent**: leave the repo consistent and verified.
- **Expected outcomes**: README section for the fleet UI (install, run, step language, buttons, security
  notes including that the UI itself has no login and must stay on loopback or a trusted host); dependency
  versions pinned to latest stable and scanned; Pi-friendly install (prebuilt wheels only).
- **Todo List**:
  1. README: "Fleet UI" section.
  2. Pin latest stable versions of Streamlit and the HTTP client; run `pip-audit`; check aarch64 wheels exist
     for Python 3.12 with `pip download --only-binary`.
  3. Run the complete test suite (existing 36 + new) and a manual smoke run of `streamlit run`.
- **Relevant Context**: `README.md`, `tello_api/requirements*.txt`, `Dockerfile` (Python 3.12 base),
  `.gitignore`.
- **Status**: `[ ] pending`

## Non-goals
- Per-drone different scripts, loops/conditionals, saving/loading scripts, container image for the UI,
  emergency motor cut-off, authentication for the Streamlit page itself.
