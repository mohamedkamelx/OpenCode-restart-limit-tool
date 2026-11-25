import { execFile } from "node:child_process"
import { promisify } from "node:util"

import { Plugin } from "@opencode/plugin"

const run = promisify(execFile)

const DEFAULT_REPO = "D:\\Vpn\\MYIP\\OpenCode-restart-limit-tool"
const DEFAULT_WANS = "INTERNET_TR069_R_PTM"

const LIMIT_PATTERNS = [
  /insufficient[-_\s]?quota/i,
  /quota[-_\s]?exceeded/i,
  /budget exceeded/i,
  /usage limit/i,
  /\blimit (has been|was)? reached/i,
  /rate[-_\s]?limit/i,
  /too many requests/i,
  /out of credits/i,
  /credits (exhausted|exceeded)/i,
  /exceeded your (plan|usage)?\s*limit/i,
  /reached your (usage)?\s*limit/i,
  /payment required/i,
  /\b402\b/,
  /\b429\b/,
]

function isLimitError(text) {
  if (!text) return false
  return LIMIT_PATTERNS.some((re) => re.test(text))
}

function stringifyError(error) {
  if (!error) return ""
  if (typeof error === "string") return error
  if (error instanceof Error) return error.message || String(error)
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

function isLimitLikeMessage(msg) {
  const info = msg?.info || msg
  if (!info || info.role !== "assistant") return false
  if (info.error) {
    if (isLimitError(stringifyError(info.error))) return true
  }
  if (info.finish === "error") {
    const text = (msg.parts || [])
      .filter((p) => p?.type === "text")
      .map((p) => p.text || "")
      .join("\n")
    if (isLimitError(text)) return true
  }
  return false
}

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

async function runRouterChange(repo, python, wans, settle, ipWait, timeout, dryRun) {
  const args = [
    "-c", PY, repo, wans,
    String(settle), String(ipWait), String(timeout),
    dryRun ? "1" : "0",
  ]
  const { stdout, stderr } = await run(python, args, { cwd: repo, timeout: 180_000 })
  const lines = stdout.trim().split(/\r?\n/)
  const last = lines[lines.length - 1] || ""
  try {
    return JSON.parse(last)
  } catch {
    return { output: stdout.trim(), stderr: stderr.trim() || undefined }
  }
}

export default Plugin.define({
  id: "zte_router_ip",
  async setup(ctx) {
    const repo = String(ctx.options?.repo || DEFAULT_REPO)
    const python = String(ctx.options?.python || "python")
    const defaultWans = String(ctx.options?.wans || DEFAULT_WANS)
    const autoContinue = ctx.options?.autoContinue !== false
    const continuePrompt = String(ctx.options?.prompt || "continue")
    const settle = Number(ctx.options?.settle ?? 5)
    const ipWait = Number(ctx.options?.ipWait ?? 45)
    const timeoutSec = Number(ctx.options?.timeout ?? 60)
    const cooldownMs = Number(ctx.options?.cooldownMs ?? 120_000)

    const recent = new Map()

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
          const s = Number(input.settle ?? settle)
          const iw = Number(input.ip_wait ?? ipWait)
          const to = Number(input.timeout ?? timeoutSec)
          try {
            const result = await runRouterChange(repo, python, wans, s, iw, to, dryRun)
            return { content: JSON.stringify(result, null, 2) }
          } catch (error) {
            return { content: `router change failed: ${error.stderr || error.message}` }
          }
        },
      })
    })

    if (!autoContinue) return

    const controller = new AbortController()
    ;(async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const type = event?.type
          const props = event?.properties || {}
          const sessionID = props?.sessionID || props?.session?.id || props?.info?.sessionID
          if (!sessionID) continue

          const isIdle =
            type === "session.idle" ||
            type === "session.status" && props?.status?.type === "idle"

          if (!isIdle) continue

          const now = Date.now()
          const last = recent.get(sessionID) || 0
          if (now - last < cooldownMs) continue
          recent.set(sessionID, now)

          try {
            const messages = await ctx.session.context({ sessionID })
            const list = Array.isArray(messages) ? messages : messages?.messages || []
            if (!list.length) continue
            const lastMsg = list[list.length - 1]
            if (!isLimitLikeMessage(lastMsg)) continue

            await runRouterChange(repo, python, defaultWans, settle, ipWait, timeoutSec, false)
            await ctx.session.prompt({ sessionID, text: continuePrompt, delivery: "queue" })
          } catch (e) {
            // keep plugin alive
            console.error("[zte_router_ip] auto-continue failed:", e?.message || e)
          }
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          console.error("[zte_router_ip] event loop error:", e?.message || e)
        }
      }
    })()

    return () => controller.abort()
  },
})
