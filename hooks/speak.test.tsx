import { expect, test } from 'claude-code/testing'

// The agent's ask goes into the chat as the person's words; Claude's answer goes back to the agent.
test("the agent's ask reaches the chat and Claude's answer reaches the agent", async ($, on) => {
  const posted: string[] = []
  const submitted: string[] = []
  on('command.register', () => ({ value: { command: 'call' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('env.get', (_, e) => ({ value: e.name === 'ELEVENLABS_API_KEY' ? 'k' : undefined }))
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('process.spawn', async function* () {
    yield { stream: 'stdout' as const, text: '{"port": 4242}\n{"ask": {"id": "a1", "task": "run the tests"}}\n' }
    await new Promise(() => undefined)
  })
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('http.fetch', (_, e) => {
    posted.push(`${e.url} ${e.init?.body ?? ''}`)
    return { value: { status: 200, ok: true, headers: {}, text: '{}' } }
  })
  on('turn.complete', () => ({ text: '' }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'call', args: '' })
  await new Promise(r => setTimeout(r, 50))
  expect(submitted).toContain('run the tests')

  await $.turn.complete({ reason: 'answer', answer: 'All green.' } as never)
  expect(posted.some(p => p.includes('127.0.0.1:4242/result') && p.includes('"a1"') && p.includes('All green.'))).toBe(true)
})
