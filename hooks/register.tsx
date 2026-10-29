import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Call, Mode } from '../types'

const OFF: Call = { mode: 'off', level: 0, last: '', note: '' }
const call = atom({ plugin: 'voice', key: 'call' } as const, OFF)

const SPOKEN =
  'Voice mode is on: the person is in a spoken conversation with a voice agent that hands you tasks and ' +
  'reads your final message back to them in its own words. Work exactly as usual with your tools, but end ' +
  'every turn with a one-to-three-sentence plain-language summary the agent can say out loud: no markdown, ' +
  'lists, code blocks or long paths. If you must show code, keep it in a file and describe it in words.'

// ponytail: one call per session; a second /voice hangs up
let port = 0
let ending = false
let pendingId = '' // the agent's ask that Claude is working on
let cwd = ''
let lastProgress = 0

const patch = ($: EngineInterface, p: Partial<Call>) => update($, call, c => ({ ...c, ...p }))

// Reaches the ear over its local port: the engine's fetch first, curl when the host refuses localhost.
let warned = false
const post = async ($: EngineInterface, path: string, body: object = {}) => {
  if (!port) return
  const url = `http://127.0.0.1:${port}${path}`
  const json = JSON.stringify(body)
  const viaFetch = await $.http.fetch(url, { method: 'POST', body: json }).catch(() => undefined)
  if (viaFetch?.ok) return
  const viaCurl = await $.process.run(['curl', '-s', '-m', '5', '-X', 'POST', '-d', json, url]).catch(() => undefined)
  if (viaCurl?.exitCode === 0) return
  if (!warned) {
    warned = true
    $.ui.toast(`voice: cannot reach the ear on port ${port} (${viaCurl?.stderr.trim() || 'fetch refused'})`)
  }
}

// The mod's Python environment, made on first use from requirements.txt (uv when present). It lives
// outside the mod folder: Claude Code watches that folder and reloads the mod on any write there,
// which would kill a running call.
const python = async ($: EngineInterface) => {
  const home = (await $.env.get('HOME')) ?? '/tmp'
  const venv = `${home}/.cache/claude-voice/venv`
  const py = `${venv}/bin/python`
  const ok = await $.process.run([py, '-c', 'import sherpa_onnx, pyaudio, numpy, elevenlabs']).catch(() => undefined)
  if (ok?.exitCode === 0) return py
  await patch($, { note: 'setting up Python packages (first run, about a minute)…' })
  const req = `${$.plugin.root}/requirements.txt`
  const uv = await $.process.run(['uv', '--version']).catch(() => undefined)
  const made = uv?.exitCode === 0
    ? await $.process.run(['sh', '-c', `mkdir -p "${home}/.cache/claude-voice" && uv venv -q "${venv}" && uv pip install -q -p "${py}" -r "${req}"`], { timeoutMs: 600_000 })
    : await $.process.run(['sh', '-c', `mkdir -p "${home}/.cache/claude-voice" && python3 -m venv "${venv}" && "${venv}/bin/pip" install -q -r "${req}"`], { timeoutMs: 600_000 })
  if (made.exitCode !== 0) throw new Error(`Python setup failed: ${made.stderr.slice(-400)}`)
  return py
}

const hangUp = async ($: EngineInterface) => {
  ending = true
  await post($, '/quit')
  port = 0
  await update($, call, () => OFF)
}

type Backend = 'local' | 'elevenlabs'

// Which voice stack: what /call was told, else what was used last, else ElevenLabs when a key is set.
const pickBackend = async ($: EngineInterface, asked: string): Promise<Backend> => {
  if (/^(local|offline)$/i.test(asked)) return 'local'
  if (/^(eleven|elevenlabs|cloud)$/i.test(asked)) return 'elevenlabs'
  const saved = await $.store.get('backend')
  if (saved === 'local' || saved === 'elevenlabs') return saved
  return (await $.env.get('ELEVENLABS_API_KEY')) ? 'elevenlabs' : 'local'
}

const start = async ($: EngineInterface, asked = '') => {
  const backend = await pickBackend($, asked)
  const key = await $.env.get('ELEVENLABS_API_KEY')
  if (backend === 'elevenlabs' && !key) {
    $.ui.toast('voice: set ELEVENLABS_API_KEY, or use /call local')
    return
  }
  await $.store.set('backend', backend)
  await update($, call, () => ({ ...OFF, mode: 'starting' as Mode, backend }))
  ending = false
  let py: string
  try {
    py = await python($)
  } catch (e) {
    await update($, call, () => OFF)
    $.ui.toast(`voice: ${e instanceof Error ? e.message : String(e)}`)
    return
  }
  const env: Record<string, string> = key ? { ELEVENLABS_API_KEY: key } : {}
  const voice = await $.env.get('ELEVENLABS_VOICE_ID')
  if (voice) env.ELEVENLABS_VOICE_ID = voice
  const agent = (await $.env.get('ELEVENLABS_AGENT_ID')) ?? (await $.store.get('agentId'))
  if (typeof agent === 'string' && agent) env.ELEVENLABS_AGENT_ID = agent
  if (cwd) env.VOICE_PROJECT = cwd

  void (async () => {
    const ear = $.process.spawn({ argv: [py, '-I', `${$.plugin.root}/hooks/${backend === 'local' ? 'local.py' : 'agent.py'}`], env })
    let buf = ''
    let lastDraw = 0
    let errTail = ''
    let exit: { code: number | null; signal: string | null } | undefined
    const it = ear[Symbol.asyncIterator]()
    try {
      while (true) {
        const step = await it.next()
        if (step.done) {
          exit = step.value
          break
        }
        const { stream, text } = step.value
        if (stream === 'stderr') {
          errTail = (errTail + text).slice(-600)
          continue
        }
        buf += text
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          let msg: { port?: number; agent_id?: string; mode?: string; level?: number; you?: string; voice?: string; ask?: { id: string; task: string }; note?: string; error?: string }
          try {
            msg = JSON.parse(line)
          } catch {
            continue
          }
          if (msg.port) {
            port = msg.port
            await patch($, { mode: 'listening', note: '' })
          } else if (msg.agent_id) {
            await $.store.set('agentId', msg.agent_id)
          } else if (msg.you) {
            $.ui.log(`🎙 you: ${msg.you}`)
          } else if (msg.voice) {
            $.ui.log(`🔊 voice: ${msg.voice}`)
            await patch($, { last: msg.voice })
          } else if (msg.ask) {
            pendingId = msg.ask.id
            await patch($, { mode: 'thinking' })
            void $.prompt.submit({ text: msg.ask.task, asUser: true })
          } else if (msg.note !== undefined) {
            await patch($, { note: msg.note })
          } else if (msg.error) {
            $.ui.toast(`voice: ${msg.error}`)
          } else if (msg.mode) {
            const now = Date.now()
            const mode: Mode = msg.mode === 'speaking' ? 'speaking' : pendingId ? 'thinking' : 'listening'
            const c = await read($, call)
            // ponytail: redraw at most ~8 fps and only when the orb would visibly move
            if (c.mode !== mode || (now - lastDraw > 120 && Math.abs((msg.level ?? 0) - c.level) > 0.02)) {
              lastDraw = now
              await patch($, { mode, level: msg.level ?? 0 })
            }
          }
        }
      }
    } catch (e) {
      errTail += ` | mod: ${e instanceof Error ? e.message : String(e)}`
    } finally {
      port = 0
      await update($, call, () => OFF)
      const why = exit ? `exit ${exit.code ?? exit.signal}` : 'stream ended'
      const home = (await $.env.get('HOME')) ?? '/tmp'
      void $.fs.write(`${home}/.cache/claude-voice/last-call.log`, `${new Date().toISOString()} ${backend} ${why}\n${errTail.replace(/.*resampler.*\n?|.*sample_rate.*\n?/g, '')}\n`).catch(() => undefined)
      if (!ending) $.ui.toast(`voice: the call ended (${why})`)
    }
  })()
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    cwd = e.cwd
    // A reload (an edit to the mod) ends any call with it: the state must not say one is still on.
    await update($, call, () => OFF)
    await $.command.register({ name: 'call', description: 'Start or end a spoken call with this chat (/call local for the offline voice, /call eleven for ElevenLabs)', argumentHint: '[local|eleven]' })
    return next(e)
  })

  on('command.run', { command: 'call' }, async ($, e) => {
    const c = await read($, call)
    if (c.mode === 'off') {
      await start($, e.args.trim())
      return { text: 'Voice call starting. Say something; /call again (or the ■ above the prompt) ends it.' }
    }
    await hangUp($)
    return { text: 'Voice call ended.' }
  })

  on('prompt.compose', async ($, e, next) => {
    const out = await next(e)
    if ((await read($, call)).mode === 'off') return out
    return { sections: [...out.sections, { id: 'voice:spoken', text: SPOKEN, scope: 'session' as const }] }
  })

  // While on a call, what happens in the chat is part of the same conversation: typed prompts and the
  // answers to them reach the voice as context, and each tool call while it waits becomes a progress line.
  on('prompt.submit', async ($, e, next) => {
    if (port && e.origin.kind !== 'plugin' && e.text.trim()) {
      void post($, '/note', { text: `[chat] The user just typed in the chat: ${e.text.slice(0, 600)}` })
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const now = Date.now()
    if (port && pendingId && e.agentId === undefined && now - lastProgress > 2500) {
      lastProgress = now
      void post($, '/progress', { text: describe(e) })
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && port) {
      const answer = e.reason === 'answer' && e.answer.trim() ? e.answer.slice(0, 4000) : `Claude Code's turn ended without an answer (${e.reason}).`
      if (pendingId) {
        const id = pendingId
        pendingId = ''
        await patch($, { mode: 'listening' })
        void post($, '/result', { id, text: answer })
      } else if (e.reason === 'answer' && e.answer.trim()) {
        void post($, '/note', { text: `[chat] You just answered in the chat: ${e.answer.slice(0, 800)}` })
      }
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)
    if (e.props.hasSurvey) return below
    const c = await read($, call)
    if (c.mode === 'off') return below // no call, no orb: the band stays free
    const { Box, Button, Svg, Text } = $.ui.resolve(e)
    const status = c.mode === 'starting' ? c.note || 'connecting…' : c.mode === 'thinking' ? 'Claude is on it' : c.mode === 'speaking' ? 'speaking' : 'listening'
    const toggle = () => hangUp($)

    // The orb is the picture; the one control beside it is a bare glyph button (the only pressable
    // element this UI offers is a text Button, so a click target cannot be drawn as the circle itself).
    const label = '■'
    return (
      <Box flexDirection="row" alignItems="center" columnGap={1} paddingX={1}>
        {e.surface === 'desktop' ? (
          <Svg source={orb(c.mode, c.level)} alt={`voice orb: ${status}`} width={56} height={56} isInteractive />
        ) : (
          <Text>{glyph(c.mode)}</Text>
        )}
        <Button key="orb-press" plain dimColor label={label} onPress={toggle} />
        {c.note && c.mode !== 'listening' ? <Text dimColor>{c.note}</Text> : null}
        {below}
      </Box>
    )
  })
}

// One short line about a tool call, for the voice's progress: the tool and the thing it touches.
const describe = (e: { tool: string } & Record<string, unknown>) => {
  const arg = ['file_path', 'command', 'pattern', 'path', 'url', 'query', 'description', 'prompt'].map(k => e[k]).find(v => typeof v === 'string') as string | undefined
  const what = arg ? arg.replace(/\s+/g, ' ').slice(0, 80) : ''
  const verbs: Record<string, string> = { Read: 'reading', Edit: 'editing', Write: 'writing', Bash: 'running', Grep: 'searching for', Glob: 'looking for files', WebFetch: 'fetching', WebSearch: 'searching the web for', Agent: 'delegating' }
  return `${verbs[e.tool] ?? `using ${e.tool} on`} ${what}`.trim()
}

const glyph = (mode: Mode) => (mode === 'speaking' ? '🔊' : mode === 'thinking' ? '💭' : mode === 'starting' ? '⏳' : mode === 'off' ? '📞' : '🎙')

// The ElevenLabs orb: a dark glass sphere with a soft four-petal clover of colour drifting inside it.
// Colours are the originals (listening blue, talking warm, idle grey); the clover spins slowly, breathes,
// and swells with the mic while listening.
const PALETTE: Record<string, [string, string]> = {
  listening: ['#CADCFC', '#A0B9D1'],
  speaking: ['#F6E7D8', '#E0CFC2'],
  thinking: ['#E5E7EB', '#9CA3AF'],
  starting: ['#E5E7EB', '#9CA3AF'],
  off: ['#CADCFC', '#A0B9D1'],
}

export const orb = (mode: Mode, level: number) => {
  const [c1, c2] = PALETTE[mode]
  const spin = mode === 'speaking' ? '7s' : mode === 'off' ? '26s' : '14s'
  const breathe = mode === 'speaking' ? '0.9s' : '3.2s'
  const swell = mode === 'listening' ? 1 + Math.min(0.35, level * 1.2) : 1
  const glow = mode === 'off' ? 0.45 : mode === 'thinking' || mode === 'starting' ? 0.8 : 1
  const petal = (deg: number, rx: number, ry: number, d: number) =>
    `<ellipse cx="32" cy="${32 - d}" rx="${rx}" ry="${ry}" fill="url(#petal)" transform="rotate(${deg} 32 32)"/>`
  const clover = [22, 112, 202, 292].map(a => petal(a, 7, 10.5, 11)).join('')
  const inner = [0, 120, 240].map(a => petal(a + 60, 5.5, 8, 7)).join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
<defs>
  <clipPath id="c"><circle cx="32" cy="32" r="27"/></clipPath>
  <radialGradient id="petal" cx="50%" cy="40%" r="58%">
    <stop offset="0" stop-color="#ffffff" stop-opacity="0.95"/><stop offset="0.3" stop-color="${c1}"/><stop offset="0.7" stop-color="${c2}"/><stop offset="1" stop-color="${c2}" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="sphere" cx="38%" cy="30%" r="80%"><stop offset="0" stop-color="#1b1c20"/><stop offset="1" stop-color="#050507"/></radialGradient>
  <radialGradient id="vig" cx="50%" cy="50%" r="50%"><stop offset="0.6" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity="0.8"/></radialGradient>
  <radialGradient id="rim" cx="50%" cy="50%" r="50%"><stop offset="0.9" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#5a5c63" stop-opacity="0.55"/></radialGradient>
  <filter id="soft" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="1.4"/></filter>
</defs>
<circle cx="32" cy="32" r="29" fill="#121316"/>
<circle cx="32" cy="32" r="27" fill="url(#sphere)"/>
<g clip-path="url(#c)">
  <g transform="translate(32 32) scale(${swell.toFixed(3)}) translate(-32 -32)" opacity="${glow}">
    <g filter="url(#soft)">
      <g>
        <animateTransform attributeName="transform" type="rotate" from="0 32 32" to="360 32 32" dur="${spin}" repeatCount="indefinite"/>
        ${clover}
      </g>
      <g opacity="0.7">
        <animateTransform attributeName="transform" type="rotate" from="360 32 32" to="0 32 32" dur="${spin}" repeatCount="indefinite"/>
        ${inner}
      </g>
    </g>
    <animateTransform attributeName="transform" type="scale" additive="sum" values="1;1.06;1" dur="${breathe}" repeatCount="indefinite"/>
  </g>
  <circle cx="32" cy="32" r="27" fill="url(#vig)"/>
  <circle cx="32" cy="32" r="27" fill="url(#rim)"/>
</g>
</svg>`
}
