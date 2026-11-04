STATUS: READY-FOR-LIVE

# Lane gl-gateway: `hermes-api` bridge brain

Branch `bridge/gateway-brain`. Scope held to `bridge/` plus a short README
paragraph. No iOS changes.

## What changed

- `bridge/hermes_bridge.py`
  - New brain `HERMES_BRIDGE_BRAIN=hermes-api`. `hermes` (CLI) is still the
    default; the provider brains are unchanged.
  - Config: `HERMES_API_URL` (default `http://127.0.0.1:8642/v1/chat/completions`),
    `HERMES_API_KEY` (read from the environment at call time and never held
    in a module global), `HERMES_API_TIMEOUT` (default 180 s, used for text
    and photo turns alike).
  - `check_brain_config()`: `main()` exits with a clear FATAL line if
    `hermes-api` is selected and no key is set. The message never contains
    secret values.
  - `build_hermes_api_request()` / `_post_hermes_api()` / `ask_hermes_api()`
    use stdlib `urllib` and run through `asyncio.to_thread`. The body is
    `{"model":"hermes","stream":false,"messages":[<one user message>]}`. A
    photo turn sends a text part plus an `image_url` data URI, built from the
    photo bytes with no temp file. Headers: Bearer, Content-Type and
    `X-Hermes-Session-Id`, on every request including the first.
  - Sessions: `new_api_session_id()` makes `glasses-<YYYYMMDD>-<uuid4 hex[:12]>`.
    `load_api_session` / `store_api_session` / `clear_api_session` follow the
    CLI's same-day rule but use their own file
    (`~/.hermes_glasses_bridge_api_session.json`), so a CLI `local-…` id is
    never sent to the API. A first turn gets `VOICE_PERSONA`, as with the CLI
    brain. `new_session` from the app clears the API store too. The id is
    saved only after a 200 response.
  - Errors:
    - 409: one retry with a fresh id (persona included), and that id is
      stored.
    - Other HTTP errors give "Hermes is unavailable right now (HTTP n).".
    - Timeouts give "Sorry, Hermes took too long to respond.".
    - Connection refused gives "Hermes is unavailable right now.".
    - A 200 with empty or unparseable content gives "Hermes didn't return a
      reply.".
    - The log shows the status plus the server's `error.message`, cut to 200
      characters, with the key redacted if the server echoes it. Headers are
      never logged.
  - Logs: `[Bridge] Asking Hermes API... (session: glasses-…)`. Both hermes
    brains now log `[Bridge] Hermes (2.1s): …`; the provider brains' log line
    is unchanged.
  - The startup banner shows `(gateway API)` for this brain.
- `bridge/test_hermes_bridge.py`: new `HermesApiBrainTests` class (16 tests)
  against a local `http.server` fake on 127.0.0.1 with a free port. No
  network is used.
- `bridge/.env.example`: documents the brain and its three env vars.
- `README.md`: one paragraph in the bridge protocol section.

## PLAN checklist

- [x] 1. `hermes-api` brain; `hermes` default; CLI and provider brains unchanged
- [x] 2. `HERMES_API_URL` / `HERMES_API_KEY` (startup error if missing, never logged or returned) / `HERMES_API_TIMEOUT` 180 s
- [x] 3. Request shape, image part, headers (session header always sent), bridge-generated ids, separate per-day store, cleared by new_session
- [x] 4. Response parsing; 409 → one retry with a fresh id that is then stored; HTTP / timeout / refused → spoken text; empty → "Hermes didn't return a reply."
- [x] 5. Log lines including elapsed seconds for both hermes brains
- [x] 6. Tests (list below)
- [x] 7. Commit, push, report

## Tests

Targeted class: `HermesApiBrainTests`, 16 tests, all OK. It covers:
- path and Bearer header
- body with exactly one message
- session header on turns 1 and 2, with the same id
- image part shape
- 409 → one retry with a new id that is stored
- a second 409 does not trigger another retry
- 500 → friendly text, log message ≤ 200 characters
- key echoed by the server gets redacted
- timeout → friendly text
- connection refused → friendly text
- empty content
- missing key: `check_brain_config` returns an error and `ask_hermes_api` returns spoken text
- `main()` exits without a key
- `new_session` (through `handle_connection`) rotates the id
- a stale-date session is not reused
- the CLI session file is byte-for-byte untouched and never sent
- id format

Every test also checks that the key appears in no returned string and in no
captured stdout.

Full bridge suite:

```
cd bridge && /tmp/gl-gateway-venv/bin/python -m unittest test_hermes_bridge -v
Ran 55 tests in 6.365s
OK
```

(39 existing + 16 new.)

## Deviations

- The suite was run with Python 3.13 from a throwaway venv
  (`/tmp/gl-gateway-venv`, plus `websockets`). On this Mac, `python3` is the
  system 3.9.6. It has no `websockets` installed, and the bridge's existing
  `str | None` annotations need 3.10 or newer (the README already says
  Python 3.11+). This was not caused by this lane.
- The connection-refused spoken text has no HTTP code ("Hermes is unavailable
  right now.") because there is no status to report.
- The README paragraph sits in the repo-root README's bridge section, not a
  new file under `bridge/`.

## Open items

- Not tested against the live gateway (forbidden in this lane). The parent
  should check on the Mac mini with
  `HERMES_API_URL=http://127.0.0.1:8642/p/hostbot/v1/chat/completions`, then
  compare the `Hermes (Xs)` log timings with the CLI brain.
- After a second 409 the stale stored id is kept. The next turn will 409
  again and retry once, which costs one extra round trip and nothing else.

## Proposed (not built)

- Stream (`stream: true`) and speak the first sentence early, to cut
  perceived latency further.
