import type { Register } from 'claude-code'
import { chooseModel, FOLLOW_UP_MS, sessionLane, type LaneName, type Previous } from './policy'

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

// Below this much context the model follows the lane freely; above it the model only moves
// up (policy.chooseModel): the prompt cache is per model, so a switch on a large context
// re-reads all of it uncached. Effort follows the lane at any size.
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
type Table = Record<string, { model?: string; effort?: string }>

const prompts = new Map<string, string>()        // turnId -> the person's text
const decisions = new Map<string, Lane | null>()  // turnId -> the lane (null: run as is)
let quietUntil = 0                                // no jev calls before this time (ms)
let previous: Previous | null = null              // the last routed turn of the main thread
let lastModel: string | undefined                 // the model the main thread last ran on
let table: Table | null = null                    // `jev lane targets`, read once
let loadedFor: string | null = null               // the session `previous` / `lastModel` belong to

// `previous` and `lastModel` outlive the process: `claude -p --continue`, `--resume` and a
// restart each start a fresh copy of this module, which would otherwise forget the session.
type Memory = Record<string, { previous: Previous | null; lastModel?: string; at: number }>
const MEMORY_KEY = 'sessions'
const MEMORY_SESSIONS = 50

async function sessionId($: any): Promise<string | null> {
  try { return await $.session.id() } catch { return null }
}

async function load($: any): Promise<void> {
  const id = await sessionId($)
  if (id === null || id === loadedFor) return
  loadedFor = id
  // This mod screens WebFetch / WebSearch here; the settings hook (`jev hook post-tool`) stands
  // down for the session instead of warning about text the mod withholds.
  await jev($, ['hook', 'mod-session'], JSON.stringify({ session_id: id }), CLASSIFY_TIMEOUT_MS)
  try {
    const kept = ((await $.store.get(MEMORY_KEY)) as Memory | undefined)?.[id]
    previous = kept?.previous ?? null
    lastModel = kept?.lastModel
  } catch {
    previous = null
    lastModel = undefined
  }
}

async function save($: any): Promise<void> {
  if (loadedFor === null) return
  try {
    const memory = ((await $.store.get(MEMORY_KEY)) as Memory | undefined) ?? {}
    memory[loadedFor] = { previous, lastModel, at: Date.now() }
    const newest = Object.entries(memory).sort(([, a], [, b]) => b.at - a.at).slice(0, MEMORY_SESSIONS)
    await $.store.set(MEMORY_KEY, Object.fromEntries(newest))
  } catch {
    // the in-process copy still holds for the rest of this process
  }
}

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
    const ran = await $.process.run(['/bin/sh', '-lc', 'exec jev "$@"', 'jev', ...args], { stdin, timeoutMs })
    if (ran.exitCode === 0 && ran.stdout.trim()) {
      const out = JSON.parse(ran.stdout)
      // A fail-open answer from jev (the backend did not answer) also starts the cool-off.
      if (out?.decision?.error === 'timeout' || out?.decision?.error === 'network') quietUntil = now + COOL_OFF_MS
      return out
    }
    if (ran.exitCode === 0) return null   // nothing to say (a hook with no output) is not a failure
  } catch (error) {
    // did not answer in time, or could not be started
    $.ui.status(`jev: unavailable (${String((error as any)?.message ?? error).slice(0, 80)})`)
  }
  quietUntil = now + COOL_OFF_MS
  return null
}

async function classify($: any, text: string): Promise<LaneName | null> {
  if (!text.trim() || text.trimStart().startsWith('/')) return null
  const out = await jev($, ['lane', 'classify', '--task', '-', '--host', 'claude-code'], text, CLASSIFY_TIMEOUT_MS)
  if (!out || !out.target || out.lane === 'keep_current') return null
  return out.lane as LaneName
}

// Every lane's model and effort, so a lane the session rules raise has its target too.
async function lanes($: any): Promise<Table> {
  if (table === null) {
    const out = await jev($, ['lane', 'targets', '--host', 'claude-code'], '', CLASSIFY_TIMEOUT_MS)
    if (out?.lanes) table = out.lanes
  }
  return table ?? {}
}

// The lane for a turn: Jev's reading, then the session rules (policy.sessionLane).
async function decide($: any, text: string): Promise<Lane | null> {
  const now = Date.now()
  const recent = previous !== null && now - previous.at <= FOLLOW_UP_MS
  if (!text.trim()) return recent && previous ? { lane: previous.lane, ...(await lanes($))[previous.lane] } : null
  const ruled = sessionLane(await classify($, text), previous, text, now)
  if (ruled.lane === null) return null
  previous = { lane: ruled.lane, at: now, corrections: ruled.corrections }
  return { lane: ruled.lane, ...(await lanes($))[ruled.lane] }
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
    const first = !decisions.has(e.turnId)
    if (first) {
      await load($)
      remember(decisions, e.turnId, await decide($, prompts.get(e.turnId) ?? ''))
    }
    const lane = decisions.get(e.turnId)
    if (!lane) {
      $.ui.status('jev: as is')
      lastModel = e.model
      if (first) await save($)
      return yield* next(e)
    }
    const wanted = lane.model ? (MODEL_IDS[lane.model] ?? lane.model) : undefined
    const model = chooseModel(wanted, lastModel ?? e.model, await contextTokens($), MODEL_SWITCH_MAX_TOKENS)
    lastModel = model
    if (first) await save($)
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
