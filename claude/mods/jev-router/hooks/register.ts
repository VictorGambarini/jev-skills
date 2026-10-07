import type { Register } from 'claude-code'

// jev-router: a cheap decision model (the `jev` command: Jev, or your own backend) makes
// three decisions inside Claude Code, where settings hooks cannot reach.
//
//   turn.step        the lane for the turn (small / medium / high / escalate) sets the
//                    effort of every step, and the model while the context is small
//   session.compact  the turns Jev marks "keep" go into the summariser's instructions
//   tool.call        WebFetch / WebSearch text that carries instructions aimed at an AI
//                    is withheld before the model reads it
//
// Every decision fails open: no answer, a timeout or `keep_current` leaves the request as
// Claude Code would have sent it. After a failure the mod stops asking for COOL_OFF_MS, so a
// backend that is down costs one timeout, not one per prompt.

// Switch the model only while the conversation is this small. The prompt cache is per
// model, so a switch on a large context re-reads all of it uncached, which can cost more
// than the bigger model reading it from cache. Effort still follows the lane above this.
const MODEL_SWITCH_MAX_TOKENS = 40_000
const CLASSIFY_TIMEOUT_MS = 6_000
const COOL_OFF_MS = 5 * 60_000

// The lane table names models the way Claude Code's agent files do; a full id passes through.
const MODEL_IDS: Record<string, string> = {
  haiku: 'claude-haiku-4-5-20251001',
  sonnet: 'claude-sonnet-5-5',
  opus: 'claude-opus-5-5',
}
const NO_EFFORT = /haiku/

type Lane = { lane: string; model?: string; effort?: string }

const prompts = new Map<string, string>()        // turnId -> the person's text
const decisions = new Map<string, Lane | null>()  // turnId -> the lane (null: run as is)
let quietUntil = 0                                // no jev calls before this time (ms)

function remember<V>(map: Map<string, V>, key: string, value: V): void {
  map.set(key, value)
  if (map.size > 50) map.delete(map.keys().next().value as string)
}

// One `jev` call: its JSON answer, or null for anything else. A failure starts the cool-off.
async function jev($: any, args: string[], stdin: string, timeoutMs: number): Promise<any> {
  const now = Date.now()
  if (now < quietUntil) return null
  try {
    // A login shell, so ~/.local/bin (where install.py links `jev`) is on PATH however
    // Claude Code was started. The arguments go through "$@", never through the shell text.
    const ran = await $.process.run({ argv: ['/bin/sh', '-lc', 'exec jev "$@"', 'jev', ...args],
                                      init: { stdin, timeoutMs } })
    if (ran.exitCode === 0 && ran.stdout.trim()) {
      const out = JSON.parse(ran.stdout)
      // A fail-open answer from jev (the backend did not answer) also starts the cool-off.
      if (out?.decision?.error === 'timeout' || out?.decision?.error === 'network') quietUntil = now + COOL_OFF_MS
      return out
    }
    if (ran.exitCode === 0) return null   // nothing to say (a hook with no output) is not a failure
  } catch {
    // did not answer in time, or could not be started
  }
  quietUntil = now + COOL_OFF_MS
  return null
}

async function classify($: any, text: string): Promise<Lane | null> {
  if (!text.trim() || text.trimStart().startsWith('/')) return null
  const out = await jev($, ['lane', 'classify', '--task', '-', '--host', 'claude-code'], text, CLASSIFY_TIMEOUT_MS)
  if (!out || !out.target || out.lane === 'keep_current') return null
  return { lane: out.lane, model: out.target.model, effort: out.target.effort }
}

async function screen($: any, tool: string, text: string): Promise<{ text: string; flagged: number } | null> {
  const out = await jev($, ['hook', 'screen-text'], JSON.stringify({ tool, text }), 15_000)
  return out?.text ? out : null
}

async function keepList($: any, messages: { role: string; content: string }[]): Promise<string[]> {
  const out = await jev($, ['compact-select'], JSON.stringify({ messages }), 30_000)
  const fates: Record<string, string> = out?.fates ?? {}
  return Object.entries(fates)
    .filter(([, fate]) => fate === 'keep')
    .map(([index]) => messages[Number(index)])
    .filter(Boolean)
    .slice(-20)
    .map(m => `- (${m.role}) ${m.content.replace(/\s+/g, ' ').slice(0, 240)}`)
}

async function contextTokens($: any): Promise<number> {
  const { context } = await $.session.usage()
  return context.tokens ?? 0
}

export const register: Register = on => {
  on('turn.start', async ($, e, next) => {
    remember(prompts, e.turnId, e.text)
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    // A subagent's steps keep the model its definition names (the lane agents included).
    if (e.agentId) return yield* next(e)
    if (!decisions.has(e.turnId)) {
      const lane = await classify($, prompts.get(e.turnId) ?? '')
      remember(decisions, e.turnId, lane)
    }
    const lane = decisions.get(e.turnId)
    if (!lane) {
      $.ui.status('jev: as is')
      return yield* next(e)
    }
    let model = e.model
    const wanted = lane.model ? (MODEL_IDS[lane.model] ?? lane.model) : undefined
    if (wanted && wanted !== e.model && (await contextTokens($)) < MODEL_SWITCH_MAX_TOKENS) model = wanted
    const effort = NO_EFFORT.test(model) ? undefined : ((lane.effort as typeof e.effort) ?? e.effort)
    $.ui.status(`jev: ${lane.lane} · ${model.replace('claude-', '')}${effort ? ' · ' + effort : ''}`)
    return yield* next({ ...e, model, effort })
  })

  on('session.compact', async ($, e, next) => {
    if (e.trigger === 'precompute' || e.agentId) return next(e)
    const messages = e.messages
      .map(m => ({ role: m.role, content: m.text }))
      .filter(m => m.content && m.content.trim())
    const keep = await keepList($, messages)
    if (!keep.length) return next(e)
    const instructions = [
      e.instructions,
      'Keep the substance of these turns in the summary (decisions, constraints, exact values, ' +
        'paths, ids, commands and unfinished work), quoting exact values where they matter:',
      ...keep,
    ].filter(Boolean).join('\n')
    $.ui.toast(`jev: ${keep.length} turns marked keep for this compaction`)
    return next({ ...e, instructions })
  })

  on('tool.call', { tool: 'WebFetch' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError || typeof ran.result?.result !== 'string') return ran
    const out = await screen($, 'WebFetch', ran.result.result)
    if (!out) return ran
    $.ui.toast(`jev: withheld ${out.flagged} part(s) of a fetched page`)
    return { ...ran, result: { ...ran.result, result: out.text } }
  })

  on('tool.call', { tool: 'WebSearch' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError || !Array.isArray(ran.result?.results)) return ran
    let withheld = 0
    const results = []
    for (const item of ran.result.results) {
      if (typeof item !== 'string') { results.push(item); continue }
      const out = await screen($, 'WebSearch', item)
      if (out) withheld += out.flagged
      results.push(out?.text ?? item)
    }
    if (!withheld) return ran
    $.ui.toast(`jev: withheld ${withheld} part(s) of search results`)
    return { ...ran, result: { ...ran.result, results } }
  })
}
