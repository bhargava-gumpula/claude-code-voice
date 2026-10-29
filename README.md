# Claude Code Voice

Talk to Claude Code out loud, like ChatGPT's voice mode, inside Claude Code.

Type `/call` and start talking. A live voice answers within a second or two, chats like a colleague, and can be
interrupted. It *is* Claude Code: ask for real work and the instruction lands in your chat, Claude Code does it
with every tool it has, and the voice tells you the outcome in its own words. Everything said is mirrored into
the chat, every task shows up as a normal message, and an animated orb above the prompt shows who is talking.

Two voice stacks, switchable per call:

| | **Local** (default) | **ElevenLabs** |
| --- | --- | --- |
| Ears | NVIDIA Parakeet, on your machine | ElevenLabs Scribe |
| Talker | Qwen 3.5 4B through Ollama | Claude Haiku 5.5 through an ElevenLabs Agent |
| Mouth | Kokoro | Eric, ElevenLabs Turbo v2 |
| Needs | Ollama + about 1 GB of models, downloaded on first call | an ElevenLabs API key |
| Cost | free, offline, private | ElevenLabs minutes |

Claude Code stays the brain either way. The local stack is a bit flatter in voice quality; ElevenLabs sounds
like a person on a phone.

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install voice --marketplace bhargava-gumpula/claude-code-voice
```

Answer `y` to add the marketplace, then pick the user scope. From a shell: `claude plugin install voice
--marketplace bhargava-gumpula/claude-code-voice`. In the desktop app, click **+** next to the prompt box,
then **Plugins**, **Add marketplace** with `bhargava-gumpula/claude-code-voice`, then **Add plugin**.

Then, for the local voice:

```bash
brew install ollama portaudio uv && ollama pull qwen3.5:4b
```

(Linux: install Ollama from ollama.com and `portaudio19-dev` from your package manager; `uv` is optional.)
The first `/call` builds a small Python environment in `~/.cache/claude-voice` and downloads the speech models (about a minute).

For ElevenLabs instead, put `ELEVENLABS_API_KEY` in the environment Claude Code runs under (the `env` block of
`~/.claude/settings.json` works for both the desktop app and the terminal). The first call creates an agent
named "Claude Code voice" in your account and keeps it up to date with this mod's prompt.

## Use

- `/call` starts a call with the stack you used last (ElevenLabs if a key is set, else local).
- `/call local` or `/call eleven` picks one; the choice sticks.
- `/call` again, or the **■** next to the orb, hangs up.
- Speak over it to interrupt. On speakers its own voice is kept out by a loudness gate, not real echo
  cancellation; if it ever cuts itself off, raise `BARGE_LEVEL` (see below).

The band above the prompt is empty when no call is on, so the mod takes no space until you use it.

## Knobs (environment variables)

| Variable | Default | What |
| --- | --- | --- |
| `VOICE_LLM` | `qwen3.5:4b` (local) / `claude-haiku-5-5` (ElevenLabs) | the talker model |
| `VOICE_SPEAKER` | `3` (af_heart) | Kokoro voice: 2 af_bella, 16 am_michael, 26 bm_george, ... |
| `ELEVENLABS_VOICE_ID` | Eric | ElevenLabs voice |
| `ELEVENLABS_AGENT_ID` | the one it created | use an agent you configured yourself |
| `BARGE_LEVEL` | `0.3` | how loud you must be to interrupt; `1.01` never |
| `VOICE_WAIT` | `6` / `9` | seconds a task may take before the voice says "still on it" and carries on |
| `VOICE_MODELS` | `~/.cache/claude-voice/models` | where the local models live |

## Troubleshooting

- **The call stays on until you end it**, however long you are quiet. If the voice program drops, it
  reconnects by itself ("Sorry, I dropped for a second"). It only gives up after three quick failures in a row.
- **The call ends right away.** The toast says why, and `~/.cache/claude-voice/last-call.log` holds the voice
  program's last output. Common causes: Ollama not running (`ollama serve`), the model not pulled
  (`ollama pull qwen3.5:4b`), or no microphone permission.
- **It hears nothing.** macOS asks for microphone access the first time; if you declined, allow Claude (or your
  terminal app) under System Settings, Privacy & Security, Microphone, then call again.
- **It cuts itself off on speakers.** Raise `BARGE_LEVEL` (0.5), or use headphones and lower it (0.2) for
  snappier interruptions.
- **Editing the mod drops the call.** Claude Code reloads a mod when its files change; start the call again.
- **Try it without a microphone:** `VOICE_BACKEND=local python3 test/live.py <project> "hey" "run the tests"`.

## How it works

`hooks/register.tsx` is the Claude Code mod: it registers `/call`, draws the orb, feeds the voice's asks into
the chat as your messages, and sends Claude's answers back. `hooks/local.py` and `hooks/agent.py` are the two
voice stacks; both speak the same tiny protocol (JSON lines out, a local HTTP port in), so adding a third is
one file. `test/live.py` runs a whole conversation by text, no microphone, with `claude -p` doing the work:

```bash
VOICE_BACKEND=local python3 test/live.py ~/some/project "hey" "what's this project about" "run the tests"
```

## License

MIT
