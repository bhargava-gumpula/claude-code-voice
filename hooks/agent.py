#!/usr/bin/env python3
"""The voice of Claude Code: an ElevenLabs Agent on this Mac's mic and speakers whose hands are Claude Code.

stdout, one JSON per line:
  {"port": N}             control port, once
  {"agent_id": "..."}     when the agent was just created (keep it)
  {"mode": "listening"|"speaking", "level": 0-1}
  {"you": "..."}          what you said        {"voice": "..."}  what it said
  {"ask": {"id", "task"}} it wants Claude Code to do something
  {"error": "..."}
Control, HTTP on 127.0.0.1:<port>, JSON bodies:
  POST /result   {"id", "text"}   Claude Code finished the ask
  POST /progress {"text"}         a line about what Claude Code is doing right now
  POST /note     {"text"}         something it should know (typed chat, context); never spoken on its own
  POST /say      {"text"}         text mode only: a typed user turn
  POST /quit
Env: ELEVENLABS_API_KEY (required), ELEVENLABS_AGENT_ID (else one is created), ELEVENLABS_VOICE_ID,
     VOICE_PROJECT (the project folder), VOICE_LLM, VOICE_TEXT=1 (no audio: a text session for tests).
"""
import array, json, math, os, signal, subprocess, sys, threading, time, urllib.error, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from elevenlabs.client import ElevenLabs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script's folder off the path
from jobs import Jobs  # noqa: E402
from elevenlabs.conversational_ai.conversation import AudioInterface, ClientTools, Conversation, ConversationInitiationData
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface

KEY = os.environ.get("ELEVENLABS_API_KEY", "")
AGENT = os.environ.get("ELEVENLABS_AGENT_ID", "")
VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "cjVigY5qzO86Huf0OWal")  # Eric: smooth, natural, conversational
LLM = os.environ.get("VOICE_LLM", "claude-haiku-5-5")  # fast, and the same family as the hands
PROJECT = os.environ.get("VOICE_PROJECT", os.getcwd())
TEXT = os.environ.get("VOICE_TEXT") == "1"
API = "https://api.elevenlabs.io/v1"
WAIT = float(os.environ.get("VOICE_WAIT", "9"))  # how long `work` holds for a quick answer before "still on it"
# Interruptions: while the agent's audio plays, mic audio only gets through above BARGE_LEVEL, so its own
# voice from the speakers (quiet at the mic) is dropped but you speaking up (loud at the mic) cuts it off.
# Calibration knob: 0.3 suits a laptop at normal volume; raise it if it interrupts itself, 1.01 = never.
BARGE_LEVEL = float(os.environ.get("BARGE_LEVEL", "0.3"))
SAMPLE_RATE = 16000

PROMPT = """You are Claude Code, talking with your user out loud. This voice is you: the same Claude Code that \
lives in their terminal and desktop app, with full access to their computer, this project, its files, the \
terminal, git, the browser, the internet and every tool a developer has. Anything Claude Code can do, you can \
do, because you are Claude Code. Never describe yourself as separate from Claude Code, never say you will "ask" \
or "send this to" Claude Code or "the system", never say you cannot see files or run things. You simply do it.

How you do things: the `work` tool is your hands. Whenever the user wants something done, checked, read, \
explained, fixed, run, tested, searched or found, say a short natural lead-in ("Sure, let me look." / "On it.") \
and call `work` with one complete instruction in plain words, including anything from the conversation it \
needs. If it returns the result, tell the user the gist right away in your own words. If it says the work is \
still going, say so in a few words and keep the conversation going; the outcome arrives later as a message that \
starts with [done], and then you tell the user what happened. The user can ask for several things in a row: \
call `work` once for each new request, right away, even while earlier ones are running; they are done in the \
order asked and each outcome comes back as its own [done] message that names the task. Say which task an \
outcome is about ("The tests are done...", "And the README..."), and never mix up or repeat one already told. \
Call `check_progress` only when they ask how things are going. You can give opinions, discuss design, brainstorm and answer general questions yourself without \
tools; use `work` for anything that touches the real project or computer.

Messages that start with [done], [progress], [chat] or [context] are your own work and your own chat window \
reporting in. They are never the user speaking: never read them back verbatim, never thank anyone for them, \
just use what they say.

Style: sound like a sharp, warm colleague on a call. Short turns, one to three sentences, even for opinions \
and general questions; offer more only if they ask. Plain spoken \
language: no lists, no markdown, no symbols, no code read aloud, no file paths unless they matter, numbers and \
names said the way a person says them. Match the user's energy and pace, be direct, skip filler and repetition, \
and ask one short question when something is unclear. Being interrupted is fine; pick up where it makes sense.

Never talk about how you work inside: no mentions of tools, hands, handing off, systems or chat windows. You \
just do things and report back. If the user goes quiet, stay quiet; never ask whether they are still there.

Honesty and care: never invent a result; only what `work` returned counts as done. Confirm before anything \
destructive or outward-facing: deleting, force-pushing, deploying, sending, paying. If something genuinely \
cannot be done, say so once, plainly, and offer the nearest thing."""

TOOLS = [
    {"type": "client", "name": "work", "expects_response": True, "response_timeout_secs": 30,
     "description": "Do something in the user's project or on their computer: read, explain, search, edit, "
                    "create, run, test, build, git, browse, anything a developer does. Returns the result, or "
                    "says the work is still going (the outcome then arrives as a [done] message).",
     "parameters": {"type": "object", "required": ["instruction"], "properties": {
         "instruction": {"type": "string", "description": "One complete instruction in plain words, with any context from the conversation it needs."}}}},
    {"type": "client", "name": "check_progress", "expects_response": True, "response_timeout_secs": 30,
     "description": "What the current piece of work is doing right now, and its result if it just finished.",
     "parameters": {"type": "object", "properties": {}}},
]

state = {"level": 0.0, "play_until": 0.0, "live": True, "conv": None, "heard_at": 0.0}
out_lock = threading.Lock()


def emit(**obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def api(method, path, body=None):
    req = urllib.request.Request(f"{API}{path}", json.dumps(body).encode() if body is not None else None,
                                 {"xi-api-key": KEY, "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{path}: HTTP {e.code} {e.read()[:400].decode(errors='replace')}") from None


def agent_config():
    return {
        "agent": {"first_message": "Hey. What are we working on?", "language": "en",
                  "prompt": {"prompt": PROMPT, "llm": LLM, "temperature": 0.3, "tools": TOOLS}},
        "tts": {"model_id": "eleven_turbo_v2", "voice_id": VOICE, "stability": 0.45, "similarity_boost": 0.85,
                "speed": 1.0, "optimize_streaming_latency": 3},
        "turn": {"mode": "turn", "turn_timeout": -1, "turn_eagerness": "normal", "silence_end_call_timeout": -1},
        "conversation": {"max_duration_seconds": 7200,
                         "client_events": ["audio", "interruption", "agent_response", "user_transcript",
                                           "agent_response_correction", "agent_tool_response"]},
    }


# lets a restarted call (VOICE_RESUME) open with a pick-up line instead of the greeting
PLATFORM = {"overrides": {"conversation_config_override": {"agent": {"first_message": True}}}}


def ensure_agent():
    """Creates the agent, or brings the existing one up to date with this file's prompt, voice and model."""
    global AGENT
    if AGENT:
        api("PATCH", f"/convai/agents/{AGENT}", {"conversation_config": agent_config(), "platform_settings": PLATFORM})
        return
    AGENT = api("POST", "/convai/agents/create", {"name": "Claude Code voice", "conversation_config": agent_config(), "platform_settings": PLATFORM})["agent_id"]
    emit(agent_id=AGENT)


# ---- Claude Code as the hands ------------------------------------------------------

def work(params):
    return jobs.work((params or {}).get("instruction", ""))


def check_progress(_params=None):
    return jobs.check()


def announce(text):
    conv = state["conv"]
    if conv:
        conv.send_user_message(text)


def idle():
    """Nobody speaking: the voice's audio has played out and the user has not talked for a moment."""
    now = time.time()
    return now > state["play_until"] + 0.6 and now - state["heard_at"] > 1.0


jobs = Jobs(emit, announce, idle, WAIT)


# ---- audio ----------------------------------------------------------------------------

class Audio(DefaultAudioInterface):
    def start(self, input_callback):
        def on_mic(chunk):
            samples = array.array("h", chunk)
            if samples:
                state["level"] = min(1.0, math.sqrt(sum(s * s for s in samples) / len(samples)) / 8000)
                if state["level"] > 0.15 and time.time() > state["play_until"] + 0.4:
                    state["heard_at"] = time.time()
            if time.time() < state["play_until"] + 0.4 and state["level"] < BARGE_LEVEL:
                chunk = bytes(len(chunk))  # ponytail: half-duplex gate, not echo cancellation
            input_callback(chunk)
        super().start(on_mic)

    def output(self, audio):
        start = max(time.time(), state["play_until"])
        state["play_until"] = start + len(audio) / 2 / SAMPLE_RATE
        super().output(audio)

    def interrupt(self):
        state["play_until"] = 0.0
        super().interrupt()


class Silent(AudioInterface):
    """Text mode: feeds silence so turn-taking runs, plays nothing."""
    def start(self, input_callback):
        self.live = True

        def feed():
            while self.live and state["live"]:
                input_callback(bytes(8000))
                time.sleep(0.25)
        threading.Thread(target=feed, daemon=True).start()

    def stop(self):
        self.live = False

    def output(self, audio):
        state["play_until"] = max(time.time(), state["play_until"]) + len(audio) / 2 / SAMPLE_RATE

    def interrupt(self):
        state["play_until"] = 0.0


def reporter():
    while state["live"]:
        emit(mode="speaking" if time.time() < state["play_until"] else "listening", level=round(state["level"], 3))
        time.sleep(0.12)


# ---- control port -----------------------------------------------------------------

def serve():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            conv = state["conv"]
            try:
                if self.path == "/result":
                    jobs.finished(body.get("id"), body.get("text", ""))
                elif self.path == "/progress":
                    jobs.progress(body.get("text", ""), body.get("id"))
                    if conv:
                        conv.send_contextual_update(f"[progress] {body.get('text', '')}")
                elif self.path == "/note" and conv:
                    conv.send_contextual_update(body.get("text", ""))
                elif self.path == "/say" and conv and TEXT:
                    emit(you=body.get("text", ""))
                    state["heard_at"] = time.time()
                    conv.send_user_message(body.get("text", ""))
                elif self.path == "/quit":
                    state["live"] = False
                    os.kill(os.getpid(), signal.SIGTERM)
            except Exception as e:  # noqa: BLE001
                emit(error=f"{self.path}: {e}")
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    emit(port=srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def project_context():
    name = os.path.basename(PROJECT.rstrip("/")) or PROJECT
    try:
        branch = subprocess.run(["git", "-C", PROJECT, "branch", "--show-current"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    return f"[context] You are working in the project \"{name}\"" + (f" on branch {branch}" if branch else "") + \
           f" (folder {PROJECT}). The user is also looking at your chat window where your work shows up."


def main():
    if not KEY:
        emit(error="ELEVENLABS_API_KEY is not set"); return 2
    srv = serve()
    try:
        ensure_agent()
        tools = ClientTools()
        tools.register("work", work)
        tools.register("check_progress", check_progress)
        conv = Conversation(
            ElevenLabs(api_key=KEY), AGENT, requires_auth=True,
            audio_interface=Silent() if TEXT else Audio(), client_tools=tools,
            config=ConversationInitiationData(conversation_config_override={"agent": {"first_message": "Sorry, I dropped for a second. I'm back."}}) if os.environ.get("VOICE_RESUME") else None,
            callback_user_transcript=lambda t: (state.update(heard_at=time.time()), emit(you=t)),
            callback_agent_response=lambda r: emit(voice=r),
        )
        state["conv"] = conv
        signal.signal(signal.SIGTERM, lambda *_: conv.end_session())
        threading.Thread(target=reporter, daemon=True).start()
        conv.start_session()
        time.sleep(0.5)
        conv.send_contextual_update(project_context())
        conv.wait_for_session_end()
    except Exception as e:  # noqa: BLE001
        emit(error=str(e)); return 1
    finally:
        state["live"] = False
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
