// The session rules jev-router lays over each turn's lane. Plain functions with no `$`, so
// the test kit can hold them to their word (it cannot raise `turn.step` itself).

export const LANES = ['small', 'medium', 'high', 'escalate'] as const
export type LaneName = (typeof LANES)[number]

// How long a turn still counts as the previous one's follow-up.
export const FOLLOW_UP_MS = 30 * 60_000

// The person saying the last answer did not work. Read loosely; it can only raise a lane.
const CORRECTION = new RegExp(
  "\\b(wrong|incorrect|not right|not what i|doesn'?t work|didn'?t work|not working|does not work|" +
    'still (fails?|failing|broken|wrong|not)|broke|broken|regress|try again|fix (it|this|that)|' +
    "that'?s not|you missed|same (error|problem|issue))\\b",
  'i',
)

export type Previous = { lane: LaneName; at: number; corrections: number }

export function isCorrection(text: string): boolean {
  return CORRECTION.test(text)
}

function index(lane: string): number {
  const at = LANES.indexOf(lane as LaneName)
  return at < 0 ? 0 : at
}

/**
 * The lane this turn runs in, given what Jev classified and the turn before it.
 *
 * Within FOLLOW_UP_MS of the previous turn: the lane drops at most one step a turn (a short
 * follow-up of hard work is still hard work), a correction never drops below the previous
 * lane, and a second correction in a row climbs one. Jev's own lane wins whenever it is
 * higher. Outside the window the turn stands on its own.
 */
export function sessionLane(
  classified: LaneName | null, previous: Previous | null, text: string, now: number,
): { lane: LaneName | null; corrections: number; why: string } {
  const recent = previous !== null && now - previous.at <= FOLLOW_UP_MS
  if (!recent || previous === null) return { lane: classified, corrections: 0, why: 'classified' }
  const correction = isCorrection(text)
  const corrections = correction ? previous.corrections + 1 : 0
  const prev = index(previous.lane)
  const floor = correction ? Math.min(prev + (corrections >= 2 ? 1 : 0), LANES.length - 1) : Math.max(prev - 1, 0)
  if (classified !== null && index(classified) >= floor) return { lane: classified, corrections, why: 'classified' }
  return { lane: LANES[floor], corrections, why: correction ? 'correction' : 'follow-up' }
}

const RANK: Record<string, number> = { haiku: 0, sonnet: 1, opus: 2 }

export function modelRank(model: string): number {
  const family = Object.keys(RANK).find(name => model.includes(name))
  return family === undefined ? RANK.opus : RANK[family]
}

/**
 * The model for a step. Below `maxTokens` of context the lane's model, freely. Above it the
 * model only moves up: every switch re-reads the whole context uncached (the cache is per
 * model), worth paying once for a harder lane, never for a cheaper one.
 */
export function chooseModel(wanted: string | undefined, current: string, tokens: number, maxTokens: number): string {
  if (wanted === undefined || wanted === current) return current
  if (tokens < maxTokens) return wanted
  return modelRank(wanted) > modelRank(current) ? wanted : current
}

// ── /compact-jev: keep only what Jev marks keep ─────────────────────────────

export type Msg = {
  role: string
  text: string
  toolUses?: readonly { tool_use_id: string }[]
  toolResults?: readonly { tool_use_id: string }[]
}

export const ALWAYS_KEEP_LAST = 6

/**
 * The messages `/compact-jev` keeps, in order: those Jev marked keep, the last
 * ALWAYS_KEEP_LAST, and whatever completes a kept tool call (its result) or a kept result
 * (its call), since a request with one half of a pair is refused. `fates` is indexed by
 * position in `sent`, the messages that had text to send; a message without text (a tool
 * call or result alone) stays only as the other half of a kept pair or in the tail.
 */
export function keepOnly<M extends Msg>(messages: readonly M[], sent: readonly number[], fates: Record<string, string>): M[] {
  const keep = new Set<number>()
  sent.forEach((index, j) => { if (fates[String(j)] === 'keep') keep.add(index) })
  for (let i = Math.max(0, messages.length - ALWAYS_KEEP_LAST); i < messages.length; i++) keep.add(i)
  const callAt = new Map<string, number>()
  const resultAt = new Map<string, number>()
  messages.forEach((m, i) => {
    for (const use of m.toolUses ?? []) callAt.set(use.tool_use_id, i)
    for (const result of m.toolResults ?? []) resultAt.set(result.tool_use_id, i)
  })
  let grew = true
  while (grew) {
    grew = false
    for (const i of [...keep]) {
      const m = messages[i]
      const partners = [
        ...(m.toolUses ?? []).map(u => resultAt.get(u.tool_use_id)),
        ...(m.toolResults ?? []).map(r => callAt.get(r.tool_use_id)),
      ]
      for (const j of partners) if (j !== undefined && !keep.has(j)) { keep.add(j); grew = true }
    }
  }
  return messages.filter((_, i) => keep.has(i))
}

// ── which Bash output is someone else's text ─────────────────────────────────

// A command that starts (or pipes into, or runs in a subshell) a network fetcher: its output
// is a page or an API reply, screened like WebFetch's.
const NETWORK_COMMAND = /(^|[;&|(`]|\$\()\s*(sudo\s+)?(curl|wget|xh|https?|lynx|w3m|links|aria2c|gh\s+api)\b/

export function isNetworkCommand(command: string): boolean {
  return NETWORK_COMMAND.test(command)
}
