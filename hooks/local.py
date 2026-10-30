#!/usr/bin/env python3
"""The voice of Claude Code, fully local: Parakeet ears, a small Ollama model as the talker, Kokoro mouth.

Same protocol as agent.py (the ElevenLabs backend), so the mod treats both alike:
stdout JSON lines {"port"}, {"mode","level"}, {"you"}, {"voice"}, {"ask":{"id","task"}}, {"note"}, {"error"};
HTTP POST /result {"id","text"}  /progress {"text"}  /note {"text"}  /say {"text"} (text mode)  /quit.
Env: VOICE_PROJECT, VOICE_TEXT=1, VOICE_LLM (Ollama model, default qwen3.5:4b), VOICE_SPEAKER (Kokoro id),
     OLLAMA_HOST, VOICE_MODELS (folder, default ~/.cache/claude-voice/models), BARGE_LEVEL, VOICE_WAIT, VOICE_RESUME.
"""
import collections, json, os, queue, re, signal, subprocess, sys, threading, time, urllib.error, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script's folder off the path
from jobs import Jobs  # noqa: E402

PROJECT = os.environ.get("VOICE_PROJECT", os.getcwd())
TEXT = os.environ.get("VOICE_TEXT") == "1"
LLM = os.environ.get("VOICE_LLM", "qwen3.5:4b")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
MODELS = os.path.expanduser(os.environ.get("VOICE_MODELS", "~/.cache/claude-voice/models"))
SPEAKER = int(os.environ.get("VOICE_SPEAKER", "3"))  # Kokoro v1.0: 3 af_heart, 2 af_bella, 16 am_michael, 26 bm_george
WAIT = float(os.environ.get("VOICE_WAIT", "6"))
# Interruptions: while the voice plays, mic audio above this level cuts it off (your voice is loud at the
# mic, its own playback from the speakers is quiet). Calibration knob: raise it if it interrupts itself.
BARGE_LEVEL = float(os.environ.get("BARGE_LEVEL", "0.3"))
RATE = 16000
ASR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
TTS = "kokoro-multi-lang-v1_0"
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

PROMPT = """You are Claude Code, talking with your user out loud. This voice is you: the same Claude Code that \
lives in their terminal, with full access to their computer, this project, its files, the terminal, git, the \
browser and the internet. Anything Claude Code can do, you can do, because you are Claude Code. Never say you \
are separate from Claude Code, never say you will ask or hand off to anything, never say you cannot see files \
or run things.

The `work` tool is your hands. When the user wants anything done, checked, read, explained, fixed, run, tested \
or found in the project or on the computer, say a short lead-in like "Sure, let me look" and call `work` with \
one complete instruction in plain words. The user can ask for several things in a row: call `work` once for \
each new request right away, even while earlier ones run; each call carries only that new request, never one already asked for; they are done in order and each outcome comes back as \
its own [done] message naming the task. Say which task an outcome is about, and never repeat one already told. Questions about yourself or what you can do ("can you see my files?", \
"do you have access?", "can you run code?") are not requests: answer them yourself in one sentence ("Yes, I can \
see your files, I'm right here in the project") and NEVER call `work` for them or start work nobody asked for. When it returns the result, tell the user the gist in your own words. \
If it says the work is still going, say so briefly; the outcome arrives later as a message starting with [done], \
then tell the user what happened. Small talk, opinions and general questions you answer yourself, no tools.

Messages starting with [done], [progress], [chat] or [context] are your own work and chat window reporting in, \
never the user: use them, never read them back or thank anyone.

Style: a sharp, warm colleague on a call. One to three short sentences. Plain spoken words: no lists, no \
markdown, no symbols, no code, no file paths unless they matter. Never mention tools, hands or how you work. \
If the user is quiet, stay quiet. Never invent a result. Confirm before anything destructive or outward-facing. \
If something truly cannot be done, say so once and offer the nearest thing."""

TOOLS = [
    {"type": "function", "function": {"name": "work", "description": "Do something in the user's project or on their computer: read, explain, search, edit, create, run, test, git, browse. Returns the result or says the work is still going.",
                                      "parameters": {"type": "object", "required": ["instruction"], "properties": {"instruction": {"type": "string", "description": "One complete instruction in plain words."}}}}},
    {"type": "function", "function": {"name": "check_progress", "description": "What the current piece of work is doing right now, and its result if it just finished.",
                                      "parameters": {"type": "object", "properties": {}}}},
]

LEAD_INS = ["Sure, let me look.", "On it.", "One sec, checking.", "Let me take a look."]
state = {"transcribing": 0, "asked": False, "acked": False, "level": 0.0, "play_until": 0.0, "live": True, "gen": 0, "heard_at": 0.0, "busy": threading.Lock()}
history = []
out_lock = threading.Lock()


def emit(**obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


# ---- models -------------------------------------------------------------------------

def fetch_models():
    os.makedirs(MODELS, exist_ok=True)
    need = [("silero_vad.onnx", f"{RELEASES}/asr-models/silero_vad.onnx", False),
            (ASR, f"{RELEASES}/asr-models/{ASR}.tar.bz2", True),
            (TTS, f"{RELEASES}/tts-models/{TTS}.tar.bz2", True)]
    for name, url, tar in need:
        if os.path.exists(os.path.join(MODELS, name)):
            continue
        emit(note=f"downloading {name} (first run)")
        if tar:
            subprocess.run(f"curl -sL '{url}' | tar xj", shell=True, cwd=MODELS, check=True)
        else:
            subprocess.run(["curl", "-sL", "-o", name, url], cwd=MODELS, check=True)


def load_models():
    import sherpa_onnx as s
    vad_cfg = s.VadModelConfig()
    vad_cfg.silero_vad.model = os.path.join(MODELS, "silero_vad.onnx")
    vad_cfg.silero_vad.threshold = 0.5
    vad_cfg.silero_vad.min_silence_duration = 0.6
    vad_cfg.silero_vad.min_speech_duration = 0.25
    vad_cfg.sample_rate = RATE
    vad = s.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)
    a = os.path.join(MODELS, ASR)
    asr = s.OfflineRecognizer.from_transducer(encoder=f"{a}/encoder.int8.onnx", decoder=f"{a}/decoder.int8.onnx",
                                              joiner=f"{a}/joiner.int8.onnx", tokens=f"{a}/tokens.txt",
                                              model_type="nemo_transducer", num_threads=4)
    k = os.path.join(MODELS, TTS)
    tts = s.OfflineTts(s.OfflineTtsConfig(model=s.OfflineTtsModelConfig(
        kokoro=s.OfflineTtsKokoroModelConfig(model=f"{k}/model.onnx", voices=f"{k}/voices.bin", tokens=f"{k}/tokens.txt",
                                             data_dir=f"{k}/espeak-ng-data", lexicon=f"{k}/lexicon-us-en.txt", dict_dir=f"{k}/dict"),
        num_threads=4, provider="cpu"), max_num_sentences=1))
    return vad, asr, tts


# ---- the hands -----------------------------------------------------------------------

def work(args):
    return jobs.work((args or {}).get("instruction", ""))


def check_progress(_args=None):
    return jobs.check()


def idle():
    """Nobody speaking and no exchange under way: a result can be told now."""
    now = time.time()
    return now > state["play_until"] + 0.6 and now - state["heard_at"] > 1.0 and not state["busy"].locked()


jobs = Jobs(emit, lambda text: turn(text), idle, WAIT)


# ---- the talker (Ollama) ---------------------------------------------------------

# "Can you see my files?" asks what it can do, not for work; a small model still reaches for `work`, so for
# these it gets no tools. "Can you read calc.py?" names a thing and stays a request.
CAPABILITY = re.compile(r"^(?:hey,? |so,? |um,? )*(?:can|could|do|are) you(?: even)? (?:see|access|read|reach|open|"
                        r"have access to|(?:be )?able to (?:see|access|read|use)|use)(?: all)? (?:my|the|this|your|our) "
                        r"(?:files?|project|code|codebase|repo|repository|folder|computer|terminal|screen|machine|stuff)\b"
                        r"[\w\s,']{0,20}\??$", re.I)


def ollama(messages, stream, tools=True):
    body = {"model": LLM, "messages": messages, "tools": TOOLS if tools else [], "stream": stream, "think": False,
            "keep_alive": "60m", "options": {"temperature": 0.4, "num_ctx": 8192}}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=120)


def turn(user_text, replaces=None):
    """One exchange: the user's words in, spoken sentences out, tool calls run in between.
    replaces: the words of an exchange this one continues (the user paused mid-sentence); it is dropped."""
    with state["busy"]:
        if replaces:
            for i in range(len(history) - 1, -1, -1):
                if history[i]["role"] == "user" and history[i]["content"] == replaces:
                    del history[i:]
                    break
        state["asked"] = False
        history.append({"role": "user", "content": user_text})
        gen = state["gen"]
        state["acked"] = False
        for _ in range(4):  # tool rounds
            text, calls = speak_stream(gen, tools=not CAPABILITY.match(user_text.strip()))
            if gen != state["gen"]:
                return  # interrupted
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
                if not text.strip() and not state["acked"]:  # straight to work: say so, instead of a silent wait
                    say(LEAD_INS[len(history) % len(LEAD_INS)], gen)
            history.append(msg)
            if not calls:
                break
            waiting = False
            for c in calls:
                fn = c["function"]
                if fn["name"] == "work":
                    state["asked"] = True  # work went out: this exchange can no longer be redone
                out = work(fn.get("arguments")) if fn["name"] == "work" else check_progress()
                history.append({"role": "tool", "content": out, "tool_name": fn["name"]})
                if out.startswith(("Still going", "Next up")):
                    waiting = out
            if waiting:
                # a small model fills a wait with chatter: say one line and go quiet until [done]
                line = "Got it, that's next after what I'm on now." if waiting.startswith("Next up") else "Still on it, I'll tell you when it's done."
                history.append({"role": "assistant", "content": line})
                say(line, gen)
                break
        del history[:-40]


def speak_stream(gen, tools=True):
    """Streams one assistant message from Ollama, speaking each sentence as it completes."""
    text, calls, buf = "", [], ""
    first = threading.Event()

    def filler():  # a cold or busy model can take seconds: never leave dead air on a call
        if not first.wait(2.5) and gen == state["gen"] and not state["acked"]:
            say("One sec.", gen)
    threading.Thread(target=filler, daemon=True).start()
    try:
        with ollama(history_with_prompt(), True, tools) as r:
            for line in r:
                first.set()
                if gen != state["gen"]:
                    break
                d = json.loads(line)
                m = d.get("message", {})
                piece = m.get("content", "")
                if piece:
                    text += piece
                    buf += piece
                    while True:
                        cut = re.search(r"[.!?](\s|$)", buf)
                        if not cut or len(buf[:cut.end()].strip()) < 2:
                            break
                        say(buf[:cut.end()].strip(), gen)
                        buf = buf[cut.end():]
                if m.get("tool_calls"):
                    calls.extend(m["tool_calls"])
                if d.get("done"):
                    break
    except (OSError, ValueError) as e:
        emit(error=f"ollama: {e}")
        say("Sorry, my local model isn't answering right now.", gen)
    first.set()
    if buf.strip():
        say(buf.strip(), gen)
    return text, calls


def history_with_prompt():
    return [{"role": "system", "content": PROMPT + "\n\n" + project_context()}] + history


# ---- the mouth --------------------------------------------------------------------------

player = {"tts": None, "out": None}


def spoken(text):
    """Markdown and code marks read badly aloud: drop them."""
    text = re.sub(r"\[(?:done|progress|chat|context)\]\s*", "", text, flags=re.I)  # tags it copies from its inputs
    text = re.sub(r"[`*_#>]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def say(sentence, gen):
    sentence = spoken(sentence)
    if not sentence:
        return
    state["acked"] = True
    emit(voice=sentence)
    if TEXT or gen != state["gen"]:
        return
    for _ in range(50):  # the speaker opens with the mic, a moment after start
        if player["out"]:
            break
        time.sleep(0.1)
    audio = player["tts"].generate(sentence, sid=SPEAKER, speed=1.05)
    if gen != state["gen"]:
        return
    pcm = (np.clip(np.asarray(audio.samples), -1, 1) * 32767).astype(np.int16).tobytes()
    state["play_until"] = max(time.time(), state["play_until"]) + len(pcm) / 2 / audio.sample_rate
    for i in range(0, len(pcm), 4800):  # small writes so an interruption stops within ~100 ms
        out = player["out"]
        if gen != state["gen"] or not out:
            break
        try:
            out.write(pcm[i:i + 4800])
        except OSError:
            break  # speaker went away mid-sentence; listen() reopens it


def interrupt():
    state["gen"] += 1
    state["play_until"] = 0.0


# ---- the ears --------------------------------------------------------------------------

def hear(asr, samples):
    """Speech to text for one VAD clip. Never raises: a bad clip must not end the call."""
    samples = np.asarray(samples, np.float32)
    if len(samples) < RATE // 4:  # blips under 0.25s are clicks, not words
        return ""
    samples = np.concatenate([samples, np.zeros(RATE // 2, np.float32)])  # tail room for the encoder
    try:
        st = asr.create_stream()
        st.accept_waveform(RATE, samples)
        asr.decode_stream(st)
        return st.result.text.strip()
    except Exception as e:
        print(f"asr skipped a clip: {e}", file=sys.stderr, flush=True)
        return ""


# Turn-taking. VAD ends a clip after 0.6s of silence, but people pause mid-sentence ("check the... readme")
# and stutter. Clips are joined into one utterance; it is answered once it sounds finished, or after a longer
# pause when it does not. If the user carries on after an answer has started, the answer is dropped and the
# whole sentence answered instead, unless work already went out for it.
DANGLING = set("and or but so the a an to of with for in on at from about like because then my your our this "
               "that is are was can could would should will if when um uh er erm hmm".split())
FILLERS = re.compile(r"\b(?:um+|uh+|erm|uhm|hmm+)\b[,.]?\s*", re.I)
HOLD_DONE, HOLD_OPEN, CONTINUE_WITHIN = 0.25, 1.4, 6.0
ear = {"text": "", "due": 0.0, "last": None}  # last: {"text", "at"} of the utterance most recently answered


def clean(text):
    """Drop fillers and stuttered repeats ("the the", "I- I"): what the user meant, not every sound."""
    text = FILLERS.sub("", text)
    text = re.sub(r"\b(\w+)-\s+(?=\1)", "", text, flags=re.I)  # "t- tell" -> "tell", "I- I" -> "I"
    text = re.sub(r"\b(\w+)(?:[,]?\s+\1\b)+", r"\1", text, flags=re.I)  # "the the", "can you, can you"
    text = re.sub(r"\b(\w+ \w+)(?:[,]?\s+\1\b)+", r"\1", text, flags=re.I)
    return re.sub(r"\s+([,.?!])", r"\1", re.sub(r"\s+", " ", text)).strip(" ,")


def finished_sentence(text):
    words = re.findall(r"[a-z']+", text.lower())
    return bool(re.search(r"[.?!]\s*$", text)) and bool(words) and words[-1] not in DANGLING


def heard(said):
    """One clip of the user's speech, from the mic or (text mode) /say."""
    emit(you=said)
    now = time.time()
    state["heard_at"] = now
    interrupt()
    last = ear["last"]
    if not ear["text"] and last and now - last["at"] < CONTINUE_WITHIN and not state["asked"]:
        ear["text"], ear["replaces"] = last["text"], last["text"]  # still the same sentence: take the answer back
    ear["text"] = f'{ear["text"]} {said}'.strip()
    ear["due"] = now + (HOLD_DONE if finished_sentence(ear["text"]) else HOLD_OPEN)


def assembler():
    while state["live"]:
        time.sleep(0.05)
        now = time.time()
        if not ear["text"] or now < ear["due"] or now - state["heard_at"] < 0.2 or state["transcribing"]:
            continue
        text, replaces = ear["text"], ear.pop("replaces", None)
        ear["text"] = ""
        ear["last"] = {"text": text, "at": now}
        threading.Thread(target=turn, args=(clean(text) or text, clean(replaces) if replaces else None), daemon=True).start()


class FileMic:
    """Tests: VOICE_MIC_FILE (16 kHz mono wav) stands in for the mic, played in real time once the greeting is
    over, then silence. Nothing is played out loud."""
    def __init__(self, path):
        import wave
        with wave.open(path) as w:
            self.pcm = w.readframes(w.getnframes())
        self.i, self.t = 0, None

    def read(self, n, exception_on_overflow=False):
        if self.t is None:
            while not state["acked"] or time.time() < state["play_until"] + 0.5:
                time.sleep(0.05)
            self.t = time.time()
        self.t += n / RATE
        time.sleep(max(0.0, self.t - time.time()))
        chunk = self.pcm[self.i:self.i + 2 * n]
        self.i += 2 * n
        return chunk.ljust(2 * n, b"\0")


class NullSpeaker:
    def write(self, pcm):
        time.sleep(len(pcm) / 2 / player["tts"].sample_rate)


def listen(vad, asr):
    """Runs until you hang up. Any audio error (headphones unplugged, device switch) reopens the devices."""
    if os.environ.get("VOICE_MIC_FILE"):
        player["out"] = NullSpeaker()
        return hear_loop(vad, asr, FileMic(os.environ["VOICE_MIC_FILE"]))
    import pyaudio
    while state["live"]:
        pa = pyaudio.PyAudio()
        try:
            mic = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=512)
            player["out"] = pa.open(format=pyaudio.paInt16, channels=1, rate=player["tts"].sample_rate, output=True)
            hear_loop(vad, asr, mic)
        except Exception as e:  # noqa: BLE001
            print(f"audio error, reopening: {e}", file=sys.stderr, flush=True)
            time.sleep(1)
        finally:
            player["out"] = None
            pa.terminate()  # closes mic and speaker too


clips = queue.Queue()


def transcriber(asr):
    """Speech to text off the mic loop: a slow transcription must never stop the mic being read, or audio
    is dropped (pyaudio overflows) and words go missing."""
    while True:
        samples = clips.get()
        try:
            said = hear(asr, samples)
            if said and re.search(r"\w", said):
                heard(said)
        finally:
            state["transcribing"] -= 1


def hear_loop(vad, asr, mic):
    threading.Thread(target=transcriber, args=(asr,), daemon=True).start()
    # The VAD only says when speech starts and stops; the audio is kept here. Its own clips (vad.front) come
    # back empty or cut short in sherpa-onnx 2.0 ("circular-buffer Invalid n"), which lost whole sentences.
    last = 0.0
    pre = collections.deque(maxlen=16)  # ~0.5s before the VAD notices speech: the first syllable
    clip, talking = [], False
    while state["live"]:
        chunk = mic.read(512, exception_on_overflow=False)
        x = np.frombuffer(chunk, np.int16).astype(np.float32) / 32768
        level = float(np.sqrt(np.mean(x * x)) * 4) if len(x) else 0.0
        state["level"] = min(1.0, level)
        speaking = time.time() < state["play_until"] + 0.4
        if speaking:
            if level >= BARGE_LEVEL:
                interrupt()  # you spoke over it
            else:
                x = np.zeros_like(x)  # ponytail: half-duplex gate, not echo cancellation
        vad.accept_waveform(x)
        detected = vad.is_speech_detected()
        if detected and not speaking:
            state["heard_at"] = time.time()
        if detected and not talking:
            talking, clip = True, list(pre)
        (clip if talking else pre).append(x)
        while not vad.empty():
            vad.pop()
        if talking and (not detected or len(clip) * 512 > 30 * RATE):
            talking = False
            state["transcribing"] += 1  # the assembler waits for it
            clips.put(np.concatenate(clip))
        now = time.time()
        if now - last > 0.1:
            emit(mode="speaking" if speaking else "listening", level=round(state["level"], 3))
            last = now


# ---- control port -------------------------------------------------------------------

def serve():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if self.path == "/result":
                    jobs.finished(body.get("id"), body.get("text", ""))
                elif self.path == "/progress":
                    jobs.progress(body.get("text", ""), body.get("id"))
                elif self.path == "/note":
                    history.append({"role": "system", "content": body.get("text", "")})
                elif self.path == "/say" and TEXT:
                    heard(body.get("text", ""))
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
    return f"[context] Background, never to be mentioned unless the user asks: you are working in the project \"{name}\"" + (f" on branch {branch}" if branch else "") + f" (folder {PROJECT})."


def main():
    srv = serve()
    threading.Thread(target=assembler, daemon=True).start()
    try:
        try:
            urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3).read()
        except OSError:
            emit(error=f"Ollama is not running at {OLLAMA} (install from ollama.com, then `ollama pull {LLM}`)"); return 2
        fetch_models()
        emit(note="loading speech models")
        vad, asr, tts = load_models()
        player["tts"] = tts
        signal.signal(signal.SIGTERM, lambda *_: state.update(live=False))
        # load the talker before greeting, so the first reply is not a cold start (can take a minute on a busy Mac)
        emit(note="warming up the voice model")
        try:
            # the real system prompt and tools, so Ollama caches them and every turn skips re-reading ~1k tokens
            ollama(history_with_prompt() + [{"role": "user", "content": "hi"}], False).read()
        except OSError as e:
            emit(error=f"ollama: {e} (is `{LLM}` pulled? `ollama pull {LLM}`)"); return 2
        emit(note="")
        # VOICE_RESUME: the mod restarted a call that dropped, so pick up instead of greeting again
        greeting = "Sorry, I dropped for a second. I'm back." if os.environ.get("VOICE_RESUME") else "Hey. What are we working on?"
        history.append({"role": "assistant", "content": greeting})
        if TEXT and not os.environ.get("VOICE_MIC_FILE"):
            emit(voice=greeting)
            while state["live"]:
                time.sleep(0.2)
        else:
            import pyaudio  # noqa: F401  (fail early if missing)
            threading.Thread(target=lambda: (time.sleep(0.3), say(greeting, state["gen"])), daemon=True).start()
            listen(vad, asr)
    except Exception as e:  # noqa: BLE001
        emit(error=str(e)); return 1
    finally:
        state["live"] = False
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
