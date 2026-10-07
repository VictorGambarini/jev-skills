import type { Register } from 'claude-code'
import { chooseModel, FOLLOW_UP_MS, isNetworkCommand, keepOnly, sessionLane, type LaneName, type Previous } from './policy'

// jev-router: a cheap decision model (the `jev` command: Jev, or your own backend) makes
// four decisions inside Claude Code, where settings hooks cannot reach.
//
//   prompt.submit    the one installed skill this prompt needs, if any, as context beside it
//                    (and the lane read in the same breath, for turn.step)
//   turn.step        the lane for the turn (small / medium / high / escalate) sets the
//                    effort of every step, and the model while the context is small
//   /compact-jev     a compaction with no summariser: only the turns Jev marks "keep" stay
//                    (`/compact` itself is left exactly as Claude Code has it)
//   tool.call        text that carries instructions aimed at an AI is withheld before the
//                    model reads it: WebFetch, WebSearch, every MCP tool, and Bash commands
//                    that fetch from the network (curl, wget, gh api, ...)
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
const preclassified = new Map<string, LaneName | null>()  // prompt text -> Jev's lane, read at submit
const SKILL_TIMEOUT_MS = 10_000
let quietUntil = 0                                // no jev calls before this time (ms)
let previous: Previous | null = null              // the last routed turn of the main thread
let lastModel: string | undefined                 // the model the main thread last ran on
let table: Table | null = null                    // `jev lane targets`, read once
let loadedFor: string | null = null               // the session `previous` / `lastModel` belong to
// What the status line shows for the session (claude/statusline reads the store file).
let shown: { lane?: string; effort?: string; skill?: string; withheld: number } = { withheld: 0 }

// `previous` and `lastModel` outlive the process: `claude -p --continue`, `--resume` and a
// restart each start a fresh copy of this module, which would otherwise forget the session.
type Memory = Record<string, { previous: Previous | null; lastModel?: string; at: number;
                                lane?: string; effort?: string; skill?: string; withheld?: number }>
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
    shown = { lane: kept?.lane, effort: kept?.effort, skill: kept?.skill, withheld: kept?.withheld ?? 0 }
  } catch {
    previous = null
    lastModel = undefined
    shown = { withheld: 0 }
  }
}

async function save($: any): Promise<void> {
  if (loadedFor === null) return
  try {
    const memory = ((await $.store.get(MEMORY_KEY)) as Memory | undefined) ?? {}
    memory[loadedFor] = { previous, lastModel, at: Date.now(), ...shown }
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
  const classified = preclassified.has(text) ? (preclassified.get(text) ?? null) : await classify($, text)
  const ruled = sessionLane(classified, previous, text, now)
  if (ruled.lane === null) return null
  previous = { lane: ruled.lane, at: now, corrections: ruled.corrections }
  return { lane: ruled.lane, ...(await lanes($))[ruled.lane] }
}

// The skill suggestion `jev hook user-prompt` makes (its switch, once-per-session repeats and
// private profiles included), or null. `via: mod` tells the settings hook this one is ours.
async function suggestSkill($: any, text: string, sessionIdValue: string): Promise<string | null> {
  const event = { prompt: text, session_id: sessionIdValue, via: 'mod' }
  const out = await jev($, ['hook', 'user-prompt'], JSON.stringify(event), SKILL_TIMEOUT_MS)
  const note = out?.hookSpecificOutput?.additionalContext
  return typeof note === 'string' && note.trim() ? note : null
}

async function screen($: any, tool: string, text: string, raw = false): Promise<{ text: string; flagged: number } | null> {
  const out = await jev($, ['hook', 'screen-text'], JSON.stringify({ tool, text, raw }), 15_000)
  return out?.text ? out : null
}

const SCREEN_MIN_CHARS = 200
const SCREEN_MAX_TEXTS = 8

// Every text an MCP result carries, screened in place: a string, a list of content blocks
// (`{ type: 'text', text }`), or `{ content: [...] }`. Anything else passes as it came.
async function screenMcp($: any, tool: string, value: any, budget: { left: number; withheld: number }): Promise<any> {
  if (budget.left <= 0) return value
  if (typeof value === 'string') {
    if (value.length < SCREEN_MIN_CHARS) return value
    budget.left -= 1
    const out = await screen($, tool, value, true)
    if (!out) return value
    budget.withheld += out.flagged
    return out.text
  }
  if (Array.isArray(value)) {
    const items = []
    for (const item of value) items.push(await screenMcp($, tool, item, budget))
    return items
  }
  if (value && typeof value === 'object') {
    if (value.type === 'text' && typeof value.text === 'string') return { ...value, text: await screenMcp($, tool, value.text, budget) }
    if (Array.isArray(value.content)) return { ...value, content: await screenMcp($, tool, value.content, budget) }
  }
  return value
}

// /compact-jev, when asked for: the next `plugin` compaction is ours to answer.
let compactJevPending = false
let compactJevReport = ''
const COMPACT_JEV_MARK = '[compact-jev: keep only what Jev marks keep]'
const COMPACT_TIMEOUT_MS = 120_000

// The conversation with only what Jev marks keep (policy.keepOnly), or a reason it was not cut.
async function compactJev($: any, messages: readonly any[]): Promise<{ messages: any[] } | { skip: string }> {
  const sent: number[] = []
  const toJev: { role: string; content: string }[] = []
  messages.forEach((m, i) => {
    if (typeof m.text === 'string' && m.text.trim()) { sent.push(i); toJev.push({ role: m.role, content: m.text }) }
  })
  if (toJev.length === 0) return { skip: 'nothing to judge' }
  const out = await jev($, ['compact-select'], JSON.stringify({ messages: toJev }), COMPACT_TIMEOUT_MS)
  if (!out?.fates || out.status === 'fail_open') return { skip: 'Jev did not answer; nothing was removed' }
  if (out.status === 'partial') return { skip: 'Jev judged only part of the conversation; nothing was removed' }
  const kept = keepOnly(messages, sent, out.fates)
  if (kept.length === messages.length) return { skip: 'Jev marked every turn keep; nothing to remove' }
  const note = {
    role: 'user',
    text: `[compact-jev] Earlier parts of this conversation were removed by a decision model, which kept ` +
      `${kept.length} of ${messages.length} messages: the ones it judged to carry decisions, constraints, exact ` +
      `values or unfinished work, plus the most recent. Nothing was summarised. If something you need is ` +
      `missing, ask for it rather than guessing.`,
    toolUses: [],
  }
  return { messages: [note, ...kept] }
}

async function contextTokens($: any): Promise<number> {
  const { context } = await $.session.usage()
  return context.tokens ?? 0
}

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    const text = e.text
    if (!text.trim() || text.trimStart().startsWith('/')) return next(e)
    // Before `next`: the settings hooks run beneath it, and must already know the mod has the
    // session (load announces it), or they would suggest a skill of their own.
    await load($)
    // The skill pick and the lane are independent: both at once, so the prompt waits for the
    // slower of the two, not their sum.
    const [note, lane] = await Promise.all([suggestSkill($, text, loadedFor ?? ''), classify($, text)])
    remember(preclassified, text, lane)
    const named = note ? /`([^`]+)`/.exec(note) : null
    shown = { ...shown, skill: named ? named[1] : undefined }
    if (note) $.ui.toast(note.replace(/^\[Jev skill suggestion\] /, 'jev: ').slice(0, 120))
    return next(note ? { ...e, context: [...(e.context ?? []), note] } : e)
  })

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
      shown = { ...shown, lane: 'as is', effort: e.effort === undefined ? undefined : String(e.effort) }
      if (first) await save($)
      return yield* next(e)
    }
    const wanted = lane.model ? (MODEL_IDS[lane.model] ?? lane.model) : undefined
    const model = chooseModel(wanted, lastModel ?? e.model, await contextTokens($), MODEL_SWITCH_MAX_TOKENS)
    lastModel = model
    shown = { ...shown, lane: lane.lane, effort: effort === undefined ? undefined : String(effort) }
    if (first) await save($)
    const effort = NO_EFFORT.test(model) ? undefined : ((lane.effort as typeof e.effort) ?? e.effort)
    $.ui.status(`jev: ${lane.lane} · ${model.replace('claude-', '')}${effort ? ' · ' + effort : ''}`)
    return yield* next({ ...e, model, effort })
  })

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'compact-jev',
      description: 'Compact with no summary: keep only the turns Jev marks keep (plus the last few)',
    })
    return next(e)
  })

  // A command's own hook may not compact (the turn it holds would be compacted under it), so
  // /compact-jev queues the built-in /compact with a marker, from a timer outside any turn,
  // and the session.compact hook below answers that one compaction without the summariser.
  on('command.run', { command: 'compact-jev' }, async $ => {
    compactJevPending = true
    $.clock.after(0, () => {
      $.command.run({ command: 'compact', args: COMPACT_JEV_MARK }).catch(() => { compactJevPending = false })
    })
    return { text: 'compact-jev: asking Jev which turns to keep; nothing will be summarised.' }
  })

  // Only the compaction /compact-jev queued. /compact and auto-compaction pass untouched.
  on('session.compact', async ($, e, next) => {
    if (!compactJevPending || !(e.instructions ?? '').includes(COMPACT_JEV_MARK) || e.agentId) return next(e)
    compactJevPending = false
    const result = await compactJev($, e.messages)
    if ('skip' in result) {
      $.ui.toast(`compact-jev: ${result.skip}.`)
      return result
    }
    compactJevReport = `kept ${result.messages.length - 1} of ${e.messages.length} messages; nothing was summarised.`
    $.ui.toast(`compact-jev: ${compactJevReport}`)
    return result
  })

  // MCP tools (a browser's page text, mail, tickets) and Bash commands that fetch from the
  // network: the same withholding as WebFetch, on text that only looks like data.
  on('tool.call', async ($, e, next) => {
    const isMcp = typeof e.tool === 'string' && e.tool.startsWith('mcp__')
    const isFetch = e.tool === 'Bash' && typeof (e as any).command === 'string' && isNetworkCommand((e as any).command)
    if (!isMcp && !isFetch) return next(e)
    const ran: any = await next(e)
    if (ran.deny !== undefined || ran.isError || ran.result === undefined) return ran
    if (isFetch) {
      const stdout = ran.result?.stdout
      if (typeof stdout !== 'string' || stdout.length < SCREEN_MIN_CHARS) return ran
      const out = await screen($, 'Bash', stdout, true)
      if (!out) return ran
      shown.withheld += out.flagged
    $.ui.toast(`jev: withheld ${out.flagged} part(s) of a fetched response`)
      return { ...ran, result: { ...ran.result, stdout: out.text } }
    }
    const budget = { left: SCREEN_MAX_TEXTS, withheld: 0 }
    const result = await screenMcp($, e.tool, ran.result, budget)
    if (!budget.withheld) return ran
    shown.withheld += budget.withheld
    $.ui.toast(`jev: withheld ${budget.withheld} part(s) of ${e.tool.replace(/^mcp__/, '')}`)
    return { ...ran, result }
  })

  on('tool.call', { tool: 'WebFetch' }, async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError || typeof ran.result?.result !== 'string') return ran
    const out = await screen($, 'WebFetch', ran.result.result)
    if (!out) return ran
    shown.withheld += out.flagged
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
    shown.withheld += withheld
    $.ui.toast(`jev: withheld ${withheld} part(s) of search results`)
    return { ...ran, result: { ...ran.result, results } }
  })
}
