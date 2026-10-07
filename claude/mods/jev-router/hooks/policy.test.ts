import { test, expect } from 'claude-code/testing'
import { chooseModel, FOLLOW_UP_MS, isCorrection, sessionLane } from './policy'

const T = 1_000_000

test('a turn on its own takes the lane Jev classified', () => {
  expect(sessionLane('small', null, 'rename foo', T)).toEqual({ lane: 'small', corrections: 0, why: 'classified' })
  const stale = { lane: 'high' as const, at: T - FOLLOW_UP_MS - 1, corrections: 0 }
  expect(sessionLane('small', stale, 'rename foo', T).lane).toBe('small')
})

test('a follow-up drops one lane a turn, never more', () => {
  const after = { lane: 'high' as const, at: T - 1000, corrections: 0 }
  expect(sessionLane('small', after, 'now add a docstring', T)).toEqual({ lane: 'medium', corrections: 0, why: 'follow-up' })
  expect(sessionLane('escalate', after, 'and the rest', T).lane).toBe('escalate')
  expect(sessionLane(null, after, 'ok thanks', T).lane).toBe('medium')
})

test('a correction holds the lane, and a second climbs one', () => {
  const after = { lane: 'small' as const, at: T - 1000, corrections: 0 }
  const first = sessionLane('small', after, "that's wrong, the test still fails", T)
  expect(first).toEqual({ lane: 'small', corrections: 1, why: 'classified' })
  const second = sessionLane('small', { lane: 'small', at: T - 500, corrections: 1 }, 'still broken', T)
  expect(second).toEqual({ lane: 'medium', corrections: 2, why: 'correction' })
  const top = sessionLane('small', { lane: 'escalate', at: T - 500, corrections: 3 }, 'still wrong', T)
  expect(top.lane).toBe('escalate')
})

test('corrections are read loosely but only as corrections', () => {
  for (const text of ["that's not what I asked", 'it does not work', 'same error again', 'you missed the tests'])
    expect(isCorrection(text)).toBe(true)
  for (const text of ['add a --quiet flag', 'rename foo to bar', 'write the release notes'])
    expect(isCorrection(text)).toBe(false)
})

test('below the threshold the model follows the lane; above it only moves up', () => {
  const opus = 'claude-opus-5-5', sonnet = 'claude-sonnet-5-5', haiku = 'claude-haiku-4-5-20251001'
  expect(chooseModel(haiku, opus, 10_000, 40_000)).toBe(haiku)
  expect(chooseModel(haiku, opus, 90_000, 40_000)).toBe(opus)
  expect(chooseModel(opus, haiku, 90_000, 40_000)).toBe(opus)
  expect(chooseModel(sonnet, haiku, 90_000, 40_000)).toBe(sonnet)
  expect(chooseModel(undefined, sonnet, 90_000, 40_000)).toBe(sonnet)
})

import { keepOnly } from './policy'

test('compact-jev keeps what Jev marks keep, the tail, and both halves of a tool call', () => {
  const m = (role: string, text: string, extra: object = {}) => ({ role, text, ...extra })
  const messages = [
    m('user', 'set up the project'),                                        // 0 keep
    m('assistant', 'chatter'),                                              // 1 drop
    m('assistant', '', { toolUses: [{ tool_use_id: 't1' }] }),             // 2 call, no text
    m('user', '', { toolResults: [{ tool_use_id: 't1' }] }),               // 3 its result
    m('assistant', 'the build needs node 20', { toolUses: [{ tool_use_id: 't2' }] }), // 4 keep -> pulls 5
    m('user', '', { toolResults: [{ tool_use_id: 't2' }] }),               // 5
    m('user', 'ok'), m('assistant', 'a'), m('user', 'b'), m('assistant', 'c'), m('user', 'd'), m('assistant', 'e'),
  ]
  const sent = messages.map((x, i) => (x.text ? i : -1)).filter(i => i >= 0)   // [0,1,4,6,7,8,9,10,11]
  const fates: Record<string, string> = { '0': 'keep', '1': 'drop', '2': 'keep', '3': 'drop' }
  const kept = keepOnly(messages, sent, fates).map(x => messages.indexOf(x))
  expect(kept).toEqual([0, 4, 5, 6, 7, 8, 9, 10, 11])
})

test('compact-jev never keeps half a tool pair from the tail', () => {
  const messages = [
    { role: 'user', text: 'go' },
    { role: 'assistant', text: '', toolUses: [{ tool_use_id: 'x' }] },
    ...Array.from({ length: 5 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', text: `t${i}`,
      ...(i === 0 ? { toolResults: [{ tool_use_id: 'x' }] } : {}) })),
  ]
  const kept = keepOnly(messages, [0, 2, 3, 4, 5, 6], {}).map(x => messages.indexOf(x))
  expect(kept).toEqual([1, 2, 3, 4, 5, 6])
})

import { isNetworkCommand } from './policy'

test('Bash output is screened when the command fetches from the network', () => {
  for (const c of ['curl -s https://x.example', 'wget -qO- https://x', 'cd /tmp && curl x', 'gh api repos/a/b',
                   'sudo curl x', 'echo $(curl -s x)', 'xh GET x.example', 'https example.org'])
    expect(isNetworkCommand(c)).toBe(true)
  for (const c of ['git status', 'ls -la', 'grep curl notes.txt', 'python3 -m pytest', 'cat curl.md', 'echo "use wget"'])
    expect(isNetworkCommand(c)).toBe(false)
})
