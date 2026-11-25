import { execFile } from "node:child_process"
import { promisify } from "node:util"

import { Plugin } from "@opencode/plugin"

const run = promisify(execFile)

const DEFAULT_REPO = "D:\\Vpn\\MYIP\\OpenCode-restart-limit-tool"
const DEFAULT_WANS = "INTERNET_TR069_R_PTM"

const PY = `
import json, sys
sys.path.insert(0, sys.argv[1])
from router_restart import restart
names = [w.strip() for w in sys.argv[2].split(',') if w.strip()]
result = restart(
    names,
    print,
    settle=float(sys.argv[3]),
    ip_wait=float(sys.argv[4]),
    timeout=float(sys.argv[5]),
    dry_run=sys.argv[6] == '1',
)
print(json.dumps(result))
`.trim()

export default Plugin.define({
  id: "zte_router_ip",
  async setup(ctx) {
    const repo = String(ctx.options?.repo || DEFAULT_REPO)
    const python = String(ctx.options?.python || "python")
    const defaultWans = String(ctx.options?.wans || DEFAULT_WANS)

    await ctx.tool.transform((editor) => {
      editor.namespace({
        name: "router",
        description: "Router WAN and public IP controls",
      })
      editor.add({
        name: "change_ip",
        description:
          "Bounce the configured PPPoE WAN through the ZTE H168N router driver to get a new public IP.",
        input: {
          type: "object",
          properties: {
            dry_run: {
              type: "boolean",
              description: "Detect and report without changing the IP.",
            },
            wans: {
              type: "string",
              description: `Comma-separated WAN names (default: ${defaultWans}).`,
            },
            settle: {
              type: "number",
              description: "Seconds to wait after the redial before checking the IP.",
            },
            ip_wait: {
              type: "number",
              description: "Maximum seconds to wait for a changed public IP.",
            },
            timeout: {
              type: "number",
              description: "Maximum seconds to wait for the WAN to reconnect.",
            },
          },
          additionalProperties: false,
        },
        options: { namespace: "router", codemode: true },
        execute: async (input) => {
          const wans = String(input.wans || defaultWans)
          const dryRun = input.dry_run === true
          const settle = Number(input.settle ?? 5)
          const ipWait = Number(input.ip_wait ?? 45)
          const timeout = Number(input.timeout ?? 60)
          const args = [
            "-c", PY, repo, wans,
            String(settle), String(ipWait), String(timeout),
            dryRun ? "1" : "0",
          ]

          try {
            const { stdout, stderr } = await run(python, args, {
              cwd: repo,
              timeout: 180_000,
            })
            const lines = stdout.trim().split(/\r?\n/)
            const last = lines[lines.length - 1] || ""
            let result
            try {
              result = JSON.parse(last)
            } catch {
              result = { output: stdout.trim() }
            }
            const errorText = stderr.trim()
            return {
              content: JSON.stringify(
                errorText ? { ...result, stderr: errorText } : result,
                null,
                2,
              ),
            }
          } catch (error) {
            return {
              content: `router change failed: ${error.stderr || error.message}`,
            }
          }
        },
      })
    })
  },
})
