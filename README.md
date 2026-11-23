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
| `router_restart.py` | stable entry point used by the watcher |
| `router_kit/` | router and IP-change package |
| `router_kit/base.py` | driver/strategy interfaces, registry, plugin loader |
| `router_kit/restart.py` | selects a driver and strategy, then runs it |
| `router_kit/drivers/` | router drivers; add new routers here |
| `router_kit/drivers/huawei_dg8045.py` | built-in Huawei DG8045 driver |
| `router_kit/drivers/huawei_client.py` | low-level Huawei gateway client (legacy TLS, RSA login, CSRF, API) |
| `router_kit/strategies/` | IP-change strategies; add new methods here |
| `router_kit/strategies/pppoe_wan.py` | built-in PPPoE WAN-bounce strategy |
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
| `ROUTER_DRIVER` | `auto` | router driver: `auto`, `huawei_dg8045`, or `module:Class` |
| `ROUTER_HOST` / `ROUTER_PORT` | `192.168.1.1` / `443` | gateway address |
| `ROUTER_USER` / `ROUTER_PASS` | none (required for `--router`) | gateway login |
| `IP_CHANGE_STRATEGY` | `pppoe_wan` | IP-change strategy: `pppoe_wan` or `module:Class` |
| `ROUTER_PLUGINS` | none | comma-separated modules to import before selecting plugins |
| `ROUTER_WANS` | none (required for `--router`) | comma-separated WANs to restart, in order |
| `ROUTER_CONNECT_TIMEOUT` | 3 s | connection timeout when no gateway answers |

Router control is off by default. The built-in driver supports Huawei
DG8045-style gateways. On other networks, leave `--router` disabled or add a
driver (see below).

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
| `--router` / `--no-router` | off | change the IP before each continue |
| `--router-driver` | `ROUTER_DRIVER` from `.env`, else `auto` | router driver name or plugin path |
| `--ip-strategy` | `IP_CHANGE_STRATEGY` from `.env`, else `pppoe_wan` | IP-change strategy name or plugin path |
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

Disabled by default. With `--router`, the selected strategy runs before
resuming. The default `pppoe_wan` strategy cycles the links in `ROUTER_WANS`:

1. First WAN: disconnect, wait for the link to drop, reconnect, and wait until
   `ConnectionStatus == "Connected"`.
2. Remaining WANs: trigger a connect.
3. Wait `--router-settle` (default 5 s).
4. Poll the driver for up to `--router-ip-wait` (default 45 s) until the
   external IP differs from the captured value.
5. Send `continue`.

`router_kit/restart.py` selects the driver and strategy and runs the
sequence; `router_kit/drivers/huawei_client.py` contains the low-level
gateway client. The gateway briefly drops internet during the cycle.

## Router drivers and IP-change strategies

The router connection and the IP-change method are separate plugins. This
allows switching routers, or replacing the whole IP-change approach, without
changing the watcher.

### Drivers

A driver implements `RouterDriver` from `router_kit`:

- `connect()` and `close()`
- `external_ip()`
- `wans()` — returns a list of `{"name", "alias", "status", "raw"}` items
- `restart_wan(name, log, timeout)`
- optional `reboot(log)`

Built-in: `huawei_dg8045`. `ROUTER_DRIVER=auto` tries every registered driver
and uses the first one that connects. Select one explicitly with
`ROUTER_DRIVER=huawei_dg8045`, or load an external driver with
`ROUTER_DRIVER=my_router:MyDriver`. The module must be importable and the
attribute may be a class or a zero-argument factory.

Add a built-in driver by creating `router_kit/drivers/my_router.py` and
importing it in `router_kit/drivers/__init__.py`.

```python
from router_kit import RouterDriver, register_driver

class MyDriver(RouterDriver):
    name = "my_router"

    def connect(self):
        ...

    def close(self):
        ...

    def external_ip(self):
        ...

    def wans(self):
        ...

    def restart_wan(self, name, log, timeout=60.0):
        ...

register_driver("my_router", MyDriver)
```

### IP-change strategies

A strategy implements `IpChangeStrategy.change(driver, log, **options)` and
returns `{"before": ..., "after": ..., "changed": ...}`. Built-in:
`pppoe_wan`. Select another with `IP_CHANGE_STRATEGY=my_ip:MyStrategy`.
Strategies receive `wans`, `settle`, `ip_wait`, `timeout`, and `dry_run`.

Add a built-in strategy by creating `router_kit/strategies/my_ip.py` and
importing it in `router_kit/strategies/__init__.py`.

```python
import time

from router_kit import IpChangeStrategy, register_strategy

class RebootStrategy(IpChangeStrategy):
    name = "reboot"

    def change(self, driver, log, *, wans=None, settle=5.0, ip_wait=45.0,
               timeout=60.0, dry_run=False):
        before = driver.external_ip()
        if dry_run:
            return {"before": before, "after": before, "changed": False}

        driver.reboot(log)
        after = before
        deadline = time.time() + ip_wait
        while time.time() < deadline:
            after = driver.external_ip()
            if after and after != before:
                break
            time.sleep(3)

        return {"before": before, "after": after,
                "changed": bool(after and before and after != before)}

register_strategy("reboot", RebootStrategy)
```

The driver must override `reboot()` for this strategy.

Select plugins in `.env`:

```ini
ROUTER_DRIVER=my_router:MyDriver
IP_CHANGE_STRATEGY=my_ip:MyStrategy
```

To make external plugins available to `auto` or to a registered name, import
them first:

```ini
ROUTER_PLUGINS=my_router,my_ip
ROUTER_DRIVER=auto
IP_CHANGE_STRATEGY=reboot
```

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
