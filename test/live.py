#!/usr/bin/env python3
"""End-to-end check by text: the real ElevenLabs agent (its prompt, model and tools) with Claude Code
really doing the work (`claude -p` in a project), no microphone.

  python3 test/live.py <project-folder> "first thing to say" ["next thing" ...]
  VOICE_BACKEND=local (default: elevenlabs) picks the helper: local.py needs Ollama running.

Plays the part of the mod: every ask the agent makes is run through `claude -p` in the project and
posted back. Prints the conversation with timings. Reads ELEVENLABS_API_KEY from ~/.claude/settings.json
when it is not in the environment.
"""
import json, os, subprocess, sys, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.environ.get("VOICE_BACKEND", "elevenlabs")
AGENT = os.path.join(HERE, "..", "hooks", "local.py" if BACKEND == "local" else "agent.py")
PY = os.path.expanduser("~/.cache/claude-voice/venv/bin/python")
project, lines = sys.argv[1], sys.argv[2:]

env = dict(os.environ, VOICE_TEXT="1", VOICE_PROJECT=project)
if BACKEND != "local" and not env.get("ELEVENLABS_API_KEY"):
    env["ELEVENLABS_API_KEY"] = json.load(open(os.path.expanduser("~/.claude/settings.json")))["env"]["ELEVENLABS_API_KEY"]
store = os.path.join(HERE, ".agent_id")
if os.path.exists(store) and not env.get("ELEVENLABS_AGENT_ID"):
    env["ELEVENLABS_AGENT_ID"] = open(store).read().strip()

proc = subprocess.Popen([PY, "-I", AGENT], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
port = [0]
t0 = time.time()
quiet_at = [time.time()]
greeted = [False]


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port[0]}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def do(ask):
    print(f"  [{time.time() - t0:5.1f}s] ask -> claude -p: {ask['task']}", flush=True)
    post("/progress", {"text": "reading the project"})
    ran = subprocess.run(["claude", "-p", ask["task"], "--permission-mode", "acceptEdits", "--allowedTools", "Bash(git add:*),Bash(git commit:*),Bash(git status:*),Bash(git log:*),Bash(git diff:*),Bash(git branch:*),Bash(ls:*),Bash(wc:*),Bash(find:*)"], cwd=project,
                         capture_output=True, text=True, timeout=300)
    text = (ran.stdout or ran.stderr).strip()[-3000:]
    print(f"  [{time.time() - t0:5.1f}s] claude -p done ({len(text)} chars)", flush=True)
    post("/result", {"id": ask["id"], "text": text})


def reader():
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "port" in msg:
            port[0] = msg["port"]
        elif "agent_id" in msg:
            open(store, "w").write(msg["agent_id"])
        elif "voice" in msg:
            print(f"  [{time.time() - t0:5.1f}s] voice: {msg['voice']}", flush=True); quiet_at[0] = time.time(); greeted[0] = True
        elif "ask" in msg:
            threading.Thread(target=do, args=(msg["ask"],), daemon=True).start()
        elif "note" in msg and msg["note"]:
            print(f"  [{time.time() - t0:5.1f}s] note: {msg['note']}", flush=True)
        elif "error" in msg:
            print(f"  [{time.time() - t0:5.1f}s] ERROR: {msg['error']}", flush=True)


threading.Thread(target=reader, daemon=True).start()
while not port[0] and proc.poll() is None:
    time.sleep(0.2)
deadline = time.time() + 120  # setup + greeting; slow machines take longer
while not greeted[0] and time.time() < deadline:
    time.sleep(0.5)
for line in lines:
    print(f"[{time.time() - t0:5.1f}s] you: {line}", flush=True)
    post("/say", {"text": line})
    quiet_at[0] = time.time()
    time.sleep(3)
    while time.time() - quiet_at[0] < float(os.environ.get("QUIET", "12")):
        time.sleep(0.5)
post("/quit", {})
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
err = proc.stderr.read().strip()
if err:
    print("stderr:", err[-800:])
