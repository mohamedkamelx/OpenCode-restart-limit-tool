# agent-tools

A self-contained watcher that resumes an OpenCode session when it stops on a
usage limit.

## Requirements

- Python 3.9+
- A running OpenCode instance (tested with 2.0.11)
- Standard library only; nothing to install

Linux and macOS are supported. On Windows, omit `--detach`.

## Files

| File | Purpose |
|---|---|
| `autocontinue.py` | watcher: detects limit stops, retries with backoff, sends the prompt |
| `router_restart.py` | PPPoE restart sequence (WANs from `.env`, settle, IP check) |
| `router_client.py` | Huawei gateway client (legacy TLS, RSA login, CSRF, API) |
| `loadenv.py` | standard-library `.env` loader |
| `.env.example` | configuration template |
| `README.md` | documentation |

No credentials are stored in the tracked source files. Router values belong
in `.env`, which is gitignored.

## Setup

1. Create the local configuration file. This is only needed for router
   control; the watcher itself runs without it.

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

2. Verify server and session detection:

   ```bash
   ./autocontinue.py --status
   ```

3. Start the watcher:

   ```bash
   ./autocontinue.py
   ```

   Add `--detach` to run it in the background. The default log is
   `/tmp/opencode/autocontinue.log`.

## Configuration

`loadenv.py` reads `.env` from the script folder at startup. Existing
environment variables take precedence. Keep `.env` out of commits, archives,
and manual copies of the folder.

| Variable | Default | Use |
|---|---|---|
| `OPENCODE_SERVER_URL` | `opencode service status`, else `http://127.0.0.1:4096` | OpenCode base URL |
| `OPENCODE_SERVICE_JSON` | `~/.config/opencode/service.json` | file containing the server password |
| `OPENCODE_SESSION_ID` | auto-detect | session to watch when `--session` is omitted |
| `ROUTER_HOST` / `ROUTER_PORT` | `192.168.1.1` / `443` | gateway address |
| `ROUTER_USER` / `ROUTER_PASS` | none (required for `--router`) | gateway login |
| `ROUTER_WANS` | none (required for `--router`) | comma-separated WANs to restart, in order |
| `ROUTER_CONNECT_TIMEOUT` | 3 s | connection timeout when no gateway answers |

Router control is off by default and is specific to Huawei DG8045-style
gateways. On other networks, leave `--router` disabled.

## Usage

```bash
python3 autocontinue.py --status        # server, session, last-turn verdict
python3 autocontinue.py                 # watch the running session
python3 autocontinue.py --all           # watch any session that starts running
python3 autocontinue.py --session ses_xxx --max 3
python3 autocontinue.py --dry-run --for 60
python3 autocontinue.py --detach        # background, logs to /tmp/opencode/autocontinue.log
```

Stop the watcher with `Ctrl-C`, `SIGTERM`, or:

```bash
pkill -f autocontinue.py
```

Alternatively, start it with `--stop-file /tmp/agent.stop` and stop it with
`touch /tmp/agent.stop`.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--prompt` | `continue` | text sent back into the session |
| `--delivery` | `queue` | `queue` waits for the turn to drain; `steer` injects mid-turn |
| `--max` | 0 (unlimited) | stop after N prompts |
| `--retry-wait` | 60 s | first delay after a continue that also hits the limit |
| `--max-retry-wait` | 1800 s | cap for the doubling delay |
| `--poll` | 2 s | how often session state is checked |
| `--scan-text` | off | treat a normal turn whose text mentions a limit as a limit stop |
| `--dry-run` | off | detect and log only |
| `--router` / `--no-router` | off | cycle the PPPoE WANs before each continue |
| `--router-wans A,B` | `ROUTER_WANS` from `.env` | WANs to restart, in order |
| `--router-settle` | 5 s | wait after the restarts before checking the IP |
| `--router-ip-wait` | 45 s | maximum wait for a new external IP |
| `--router-timeout` | 60 s | maximum wait for a WAN to reconnect |

## Behaviour

```
running --(stops)--> check last assistant turn
                       |
                       +-- limit error?  -> restart router WANs
                       |                     (ROUTER_WANS, in order)
                       |                     wait 5 s + confirm new IP
                       |                     -> send "continue" to the SAME session
                       +-- anything else -> ignore, keep waiting
```

The router step runs only with `--router`. If it fails (router offline, login
error), the failure is logged and the continue is still sent.

After a continue, the watcher returns to waiting. If the continue also ends on
a limit, the next attempt is delayed by `--retry-wait`, doubling up to
`--max-retry-wait`. A stop without a limit error resets the delay.

## Router restart

Disabled by default. With `--router`, the watcher cycles the PPPoE links in
`ROUTER_WANS` before resuming:

1. First WAN: disconnect, wait for the link to drop, reconnect, and wait until
   `ConnectionStatus == "Connected"`.
2. Remaining WANs: trigger a connect.
3. Wait `--router-settle` (default 5 s).
4. Poll `system/firstupwan` for up to `--router-ip-wait` (default 45 s) until
   `ExternalIPAddress` differs from the captured value.
5. Send `continue`.

`router_restart.py` contains the sequence and `router_client.py` the gateway
client. Both are in this folder. The gateway briefly drops internet during the
cycle.

## OpenCode API

- server URL: `$OPENCODE_SERVER_URL`, else `opencode service status`
- auth: basic `opencode:<password>` from `~/.config/opencode/service.json`
- `GET /api/session/active` — active sessions
- `GET /api/session/{id}/message?limit=5&order=desc&type=assistant` — last turn
- `POST /api/session/{id}/prompt` — `{"text": "...", "delivery": "queue"}`

## Limit detection

A turn counts as a limit stop when it has `error`, `retry`, or
`finish == "error"` and the error matches:

```
insufficient[-_\s]?quota | quota[-_\s]?exceeded | budget exceeded | usage limit
limit (has been|was)? reached | rate[-_\s]?limit | too many requests
out of credits | credits (exhausted|exceeded)
exceeded your (plan|usage)?limit | reached your (usage)?limit
payment required | \b402\b | \b429\b
```

This matches the provider-error classification used by OpenCode, plus common
phrasings.

## Caveats

- If the limit is still active, the continue fails again. The backoff grows
  until the limit window resets.
- Each retry adds a user message (`continue`) to the session history.
- The session must still exist. Deleting it stops the watcher naturally.
- Without `--all`, only the selected session is watched. With `--all`, any
  session that becomes active is followed.
