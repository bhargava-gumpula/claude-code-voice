import { expect, test } from 'claude-code/testing'

// Only the person ends a call: a helper that dies is started again, picking up where it left off.
const call = async ($: Parameters<Parameters<typeof test>[1]>[0], on: Parameters<Parameters<typeof test>[1]>[1], firstExit: number) => {
  const spawns: Record<string, string>[] = []
  on('command.register', () => ({ value: { command: 'call' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('env.get', (_, e) => ({ value: e.name === 'ELEVENLABS_API_KEY' ? 'k' : undefined }))
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('fs.write', () => ({ value: undefined }))
  on('clock.after', () => ({ value: undefined }))
  on('process.spawn', async function* (_, e) {
    spawns.push((e as { env?: Record<string, string> }).env ?? {})
    yield { stream: 'stdout' as const, text: '{"port": 4242}\n' }
    if (spawns.length === 1) return { value: { code: firstExit, signal: null } }
    await new Promise(() => undefined)
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'call', args: 'eleven' } as never)
  for (let i = 0; i < 200 && spawns.length < 2; i++) await new Promise(r => setTimeout(r, 10))
  return spawns
}

test('a call that drops reconnects on its own', async ($, on) => {
  const spawns = await call($, on, 1)
  expect(spawns.length).toBe(2)
  expect(spawns[1].VOICE_RESUME).toBe('1')
})

test('a setup error (exit 2) does not retry', async ($, on) => {
  const spawns = await call($, on, 2)
  expect(spawns.length).toBe(1)
})
