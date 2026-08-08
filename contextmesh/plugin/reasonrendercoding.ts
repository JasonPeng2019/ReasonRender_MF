/**
 * ReasonRenderCoding bridge for the combined ContextMesh multi-agent demo.
 *
 * The plugin augments real OpenCode `task` calls in place. It never replaces
 * the orchestrator or worker with a one-shot tool. Any bridge failure is
 * recorded and fails open to the original worker prompt.
 */

import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"

type PluginInput = {
  $: any
}

type Resolution = {
  branch: "miss" | "hit"
  external_ref: string
  planner_tokens: number
  profile: string
  rendered_packet: Record<string, unknown>
}

function required(name: string): string {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required; launch through contextmesh/RRDdemo.sh`)
  return value
}

function appendEvent(path: string, event: Record<string, unknown>) {
  try {
    mkdirSync(dirname(path), { recursive: true })
    appendFileSync(path, JSON.stringify({ ts: Date.now(), ...event }) + "\n")
  } catch {
    // Evidence failure must not disable the actual OpenCode worker.
  }
}

export const ReasonRenderCodingPlugin = async ({ $ }: PluginInput) => ({
  "tool.execute.before": async (
    input: { tool: string; sessionID: string; callID: string },
    output: { args: any },
  ) => {
    if (input.tool !== "task") return
    const args = output.args ?? {}
    if (args.subagent_type !== "worker" || typeof args.prompt !== "string") return

    const originalPrompt = args.prompt
    let events = process.env.RRC_DEMO_EVENTS?.trim() ?? ""
    let failureID = input.callID
    try {
      const repo = required("RRC_DEMO_REPO")
      const round = required("RRC_DEMO_ROUND")
      const mode = required("RRC_DEMO_MODE")
      const database = required("RRC_DEMO_DATABASE")
      const lock = required("RRC_DEMO_LOCK")
      events = required("RRC_DEMO_EVENTS")
      const modelEvents = required("RRC_DEMO_MODEL_EVENTS")
      const everos = required("RRC_EVEROS_URL")
      const model = required("RRC_STRONG_MODEL")
      const uv = process.env.RRC_DEMO_UV_BIN?.trim() || "uv"
      if (mode !== "cold" && mode !== "warm") {
        throw new Error(`RRC_DEMO_MODE must be cold or warm, got ${mode}`)
      }

      const taskID = `${round}-${input.callID}`
      failureID = taskID
      const result = await $`env -u VIRTUAL_ENV ${uv} run python -m rrc.multiagent_demo resolve --mode ${mode} --round-id ${round} --task-id ${taskID} --task-prompt ${originalPrompt} --database ${database} --lock ${lock} --events ${events} --model-events ${modelEvents} --model ${model} --everos-url ${everos}`
        .cwd(repo)
        .quiet()
        .nothrow()
      if (result.exitCode !== 0) {
        const detail = result.stderr.toString().trim() || result.stdout.toString().trim()
        throw new Error(`RRC packet bridge exited ${result.exitCode}${detail ? `: ${detail}` : ""}`)
      }

      const parsed = JSON.parse(result.stdout.toString()) as Partial<Resolution>
      if (
        (parsed.branch !== "miss" && parsed.branch !== "hit") ||
        typeof parsed.external_ref !== "string" ||
        typeof parsed.planner_tokens !== "number" ||
        typeof parsed.profile !== "string" ||
        !parsed.rendered_packet ||
        typeof parsed.rendered_packet !== "object"
      ) {
        throw new Error("RRC packet bridge returned an invalid resolution")
      }

      output.args.prompt = [
        originalPrompt,
        "",
        `[ReasonRenderCoding] Validated read-only audit Plan + Spec (${parsed.branch.toUpperCase()}, profile=${parsed.profile}, ref=${parsed.external_ref}).`,
        "Use this packet to structure the audit. Do not edit files. The original assignment above remains authoritative.",
        JSON.stringify(parsed.rendered_packet, null, 2),
      ].join("\n")
    } catch (error) {
      appendEvent(events || ".rrc-demo-events.jsonl", {
        event: "fail_open",
        failure_id: failureID,
        source: "opencode_plugin",
        session_id: input.sessionID,
        call_id: input.callID,
        error: String(error),
      })
      // Deliberately leave output.args.prompt untouched: the real worker still
      // launches and completes the user's audit even if RRC is unavailable.
    }
  },
})
