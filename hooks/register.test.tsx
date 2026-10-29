import { expect, test } from 'claude-code/testing'

import { orb } from './register'

const BAND = { plugin: 'voice', component: 'AbovePrompt', props: { hasSurvey: false } } as const

test('off a call the band is left to the engine, and the orb swells with the mic', async ($, on) => {
  on('command.register', () => ({ value: { command: 'call' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect(await ui.find({ type: 'Button', key: 'orb-press' })).toBeFalsy()
    expect((await ui.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
    await ui.unmount()
  }

  expect(orb('listening', 0.9)).toContain('scale(1.350)')
  expect(orb('speaking', 0)).toContain('#F6E7D8')
  expect(orb('off', 0)).toContain('opacity="0.45"')
})
