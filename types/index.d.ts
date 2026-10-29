export type Mode = 'off' | 'starting' | 'listening' | 'thinking' | 'speaking'

export type Call = { mode: Mode; level: number; last: string; note: string; backend?: 'local' | 'elevenlabs' }

declare module 'claude-code' {
  interface PluginState {
    voice: { call: Call }
  }
}
