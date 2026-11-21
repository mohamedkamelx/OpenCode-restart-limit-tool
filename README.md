# agent-tools

A self-contained watcher that resumes an OpenCode session when it stops on a
usage limit.

## Files

Everything the tool needs is in this folder; nothing outside it is imported.

| File | Purpose |
|---|---|
| `autocontinue.py` | the watcher: detect limit stops, retry/backoff, prompt injection |
| `router_restart.py` | PPPoE restart sequence (VID_20, then PTM3, settle, IP check) |
| `router_client.py` | self-contained DG8045 client (legacy TLS, RSA login, CSRF, API) |
| `README.md` | this file |

## Quick start

Python 3.9+ and a running OpenCode (tested with 2.0.11) are the only
requirements. There is nothing to install — standard library only.

```bash
cd agent-tools
./autocontinue.py --status
./autocontinue.py                       # watch the running session
./autocontinue.py --router --detach --stop-file /tmp/opencode/autocontinue.stop
```

`--status` shows the server, the session it would watch, and whether the last
turn was a limit stop. `--detach` keeps it running after the terminal closes.
`--router` is only for the DG8045 setup; it is off by default.

## Sending this folder to someone else

The core watcher works as-is on another machine running OpenCode. Two parts
are tuned to the original setup and degrade safely:

| Piece | Default | On a different setup |
|---|---|---|
| Router restart | **off** unless `--router` is passed. When on: Huawei DG8045 at `192.168.1.1`, WANs `INTERNET_TR069_R_VID_20` / `..._PTM3` | Left off, nothing touches their network. If enabled and the gateway is wrong, it fails within ~3 s, is logged, and the `continue` is still sent. Set `ROUTER_HOST` and `--router-wans` for their gateway. |
| OpenCode auth | `opencode:<password>` from `~/.config/opencode/service.json` | Point `OPENCODE_SERVICE_JSON` at the right file, pass `--no-auth`, or set `OPENCODE_SERVER_URL`. |

The built-in router credentials (`vodafone` / the original password) only work
on the original LAN; override `ROUTER_USER` / `ROUTER_PASS` elsewhere.

Supported: Linux and macOS (the `--detach` fork is POSIX). Windows needs
`--detach` omitted.

### Environment variables

| Variable | Default | Use |
|---|---|---|
| `OPENCODE_SERVER_URL` | `opencode service status`, else `http://127.0.0.1:4096` | OpenCode base URL |
| `OPENCODE_SERVICE_JSON` | `~/.config/opencode/service.json` | where the server password lives |
| `OPENCODE_SESSION_ID` | auto-detect | session to watch when `--session` is omitted |
| `ROUTER_HOST` / `ROUTER_PORT` | `192.168.1.1` / `443` | gateway address |
| `ROUTER_USER` / `ROUTER_PASS` | `vodafone` / built-in | gateway login |
| `ROUTER_CONNECT_TIMEOUT` | 3 s | how fast to give up when no gateway answers |

## autocontinue.py

Watches an OpenCode session and automatically resumes it **when the turn
stopped because of a usage limit** ("usage limit", "quota exceeded",
`insufficient_quota`, `429`, "rate limit", ...).

It deliberately does *not* react to normal end-of-turn idling, so it will not
spam `continue` after every answer. Only failed turns are classified as limit
stops.

### Behaviour

```
running --(stops)--> check last assistant turn
                       |
                       +-- limit error?  -> restart router WANs
                       |                     (VID_20, then PTM3)
                       |                     wait 5 s + confirm new IP
                       |                     -> send "continue" to the SAME session
                       +-- anything else -> ignore, keep waiting
```

The router step runs only with `--router`. If it fails (router offline,
login error), the failure is logged and `continue` is still sent — the agent
must never be blocked by the network.

After a continue it goes back to waiting. If the continue also ends on a
limit, the next attempt is delayed by `--retry-wait` and the delay doubles up
to `--max-retry-wait`. A stop without a limit error resets the delay.

### Router restart (fresh IP, `--router`)

Off by default so the folder is safe to use on other networks. On the DG8045
setup, `--router` makes the watcher cycle the PPPoE link before resuming:

1. `INTERNET_TR069_R_VID_20` — disconnect, wait for the link to drop, reconnect,
   wait until `ConnectionStatus == "Connected"`.
2. `INTERNET_TR069_R_PTM3` — trigger a connect (it is normally inactive).
3. Wait `--router-settle` (default **5 s**).
4. Poll `system/firstupwan` for up to `--router-ip-wait` (default 45 s) until
   `ExternalIPAddress` differs from the captured value.
5. Send `continue`.

`router_restart.py` runs the sequence and `router_client.py` talks to the
gateway — both in this folder. The gateway briefly drops internet during the
cycle.

### Usage

```bash
cd ~/Desktop/agent-tools

python3 autocontinue.py --status        # server, session, last-turn verdict
python3 autocontinue.py                 # watch the running session
python3 autocontinue.py --all           # watch any session that starts running
python3 autocontinue.py --session ses_xxx --max 3
python3 autocontinue.py --dry-run --for 60
python3 autocontinue.py --detach        # background, logs to /tmp/opencode/autocontinue.log
```

Stop:

```bash
pkill -f autocontinue.py
# or start it with --stop-file /tmp/agent.stop and `touch /tmp/agent.stop`
```

### Options that matter

| Flag | Default | Meaning |
|---|---|---|
| `--prompt` | `continue` | text sent back into the session |
| `--delivery` | `queue` | `queue` waits for the turn to drain; `steer` injects mid-turn |
| `--max` | 0 (unlimited) | stop after N prompts |
| `--retry-wait` | 60 s | first delay after a continue that also hits the limit |
| `--max-retry-wait` | 1800 s | cap for the doubling delay |
| `--poll` | 2 s | how often session state is checked |
| `--scan-text` | off | also treat a *normal* turn whose text mentions a limit as a limit stop (may false-positive on conversations about limits) |
| `--dry-run` | off | detect and log only |
| `--router` / `--no-router` | off | cycle the PPPoE WANs before each continue |
| `--router-wans A,B` | `INTERNET_TR069_R_VID_20,INTERNET_TR069_R_PTM3` | WANs to restart, in order |
| `--router-settle` | 5 s | wait after the restarts before checking the IP |
| `--router-ip-wait` | 45 s | how long to wait for a new external IP |
| `--router-timeout` | 60 s | how long to wait for a WAN to reconnect |

### How it talks to OpenCode

- server URL: `$OPENCODE_SERVER_URL`, else `opencode service status`
- auth: basic `opencode:<password>` from `~/.config/opencode/service.json`
- `GET /api/session/active` — who is running
- `GET /api/session/{id}/message?limit=5&order=desc&type=assistant` — last turn
- `POST /api/session/{id}/prompt` — `{"text": "...", "delivery": "queue"}`

### Limit detection

A turn counts as a limit stop when it has `error`, `retry`, or
`finish == "error"` **and** the error blob matches:

```
insufficient[-_\s]?quota | quota[-_\s]?exceeded | budget exceeded | usage limit
limit (has been|was)? reached | rate[-_\s]?limit | too many requests
out of credits | credits (exhausted|exceeded)
exceeded your (plan|usage)?limit | reached your (usage)?limit
payment required | \b402\b | \b429\b
```

That is the same classification OpenCode applies to provider errors, plus the
usual phrasings.

### Caveats

- If the limit is still active, the continue will fail again. That is
  expected; the backoff grows until the window resets.
- Each retry adds a user message (`continue`) to the session history.
- The session must still exist; deleting it stops the watcher naturally
  (polls just fail).
- Only exact sessions are watched in single mode. `--all` follows any session
  that becomes active while the watcher runs.
