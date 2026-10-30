#!/usr/bin/env python3
"""Scores a voice against the bar ChatGPT's voice mode sets, scenario by scenario.

  python3 test/suite.py <project> [local|elevenlabs] [scenario ...]

Each scenario is a real call through test/live.py (spoken recordings where pauses and stutters matter, typed
lines otherwise), with `claude -p` doing the work. A judge (`claude -p`) scores each transcript 1-5 against
what ChatGPT voice would do; reply latency is measured, not judged. Writes test/results-<backend>.json.
"""
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.expanduser("~/.cache/claude-voice/venv/bin/python")
project = sys.argv[1]
backend = sys.argv[2] if len(sys.argv) > 2 else "elevenlabs"
only = set(sys.argv[3:])
AUDIO = os.path.join(HERE, ".audio")

# name: (spoken recording, or typed lines), what a ChatGPT-level voice does
SCENARIOS = {
    "small-talk": (["hey, how's it going?"],
                   "Answers warmly in one short sentence, fast, no tools, maybe asks what we're working on."),
    "identity": (["can you see my files?"],
                 "Says yes plainly as Claude Code itself (it can), in a sentence; no work, no disclaimers."),
    "stutter": ("Um, so, can you, can you tell me, uh, how many, how many commits there are?",
                "Understands it as 'how many commits', does it, and reports the number (2) without remarking on the stutter."),
    "mid-pause": ("I want you to count|2.2|the commits in this project.",
                  "Waits through the pause, treats it as one request, reports 2 commits. Does not answer 'count what?' "
                  "after the first half, or if it starts to, drops that and answers the full request."),
    "two-tasks": ("How many commits are there?|1.2|Oh, and also, what functions are in the calc file?",
                  "Takes both requests, runs each once, reports both results (2 commits; add and sub), clearly saying "
                  "which is which, never talking over itself or repeating one."),
    "long-silence": ("Hey, I'm thinking about what to do next.|25|Okay, how many commits are there?",
                     "Acknowledges briefly, then stays silent through the 25s pause (no 'are you still there?'), then "
                     "answers the question (2 commits)."),
    "opinion": (["should I use tabs or spaces in python?"],
                "Gives a direct opinion (spaces, PEP 8) in one or two sentences, no tools, no hedging lecture."),
    "correction": (["count the files in this project. actually no wait, count the commits instead."],
                   "Does only the corrected request (commits: 2), not the file count."),
}


def recording(name, spec):
    os.makedirs(AUDIO, exist_ok=True)
    path = os.path.join(AUDIO, f"{name}.wav")
    if not os.path.exists(path):
        subprocess.run([PY, os.path.join(HERE, "make_audio.py"), path, spec], check=True, capture_output=True)
    return path


def call(name, said):
    env = dict(os.environ, VOICE_BACKEND=backend, CLAUDE_MODEL=os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"))
    args = ["python3", os.path.join(HERE, "live.py"), project]
    if isinstance(said, str):
        env.update(VOICE_MIC_FILE=recording(name, said), TAIL="45")
    else:
        env.update(GAP="2", QUIET="10", TAIL="45")
        args += said
    out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=900).stdout
    return "\n".join(l for l in out.splitlines() if not re.search(r"note:|stderr|resampl", l))


def latency(transcript):
    """Seconds from each thing the user says to the voice's next line."""
    gaps, said_at = [], None
    for line in transcript.splitlines():
        m = re.match(r"\s*\[\s*([\d.]+)s\]\s*(you|heard|voice):", line)
        if not m:
            continue
        t, who = float(m.group(1)), m.group(2)
        if who in ("you", "heard"):
            said_at = t
        elif said_at is not None:
            gaps.append(round(t - said_at, 1))
            said_at = None
    return gaps


def judge(name, bar, transcript):
    prompt = f"""You are grading a voice assistant ("voice") in one test call against the bar set by ChatGPT's \
advanced voice mode: natural, brief, quick, understands messy speech (stutters, pauses, self-corrections), \
keeps track of several requests, never talks over itself, never nags, never talks about its own internals. \
The assistant is Claude Code: "ask -> claude -p" lines are it doing real work in the project; "heard:" is \
what its speech recognition got from the user's audio; "you:" is typed input. Times are seconds into the call. \
The voice's first line "Hey. What are we working on?" is its greeting.

Scenario "{name}". What a ChatGPT-level voice does here: {bar}

Transcript:
{transcript}

Reply with only JSON: {{"score": 1-5 (5 = as good as ChatGPT voice), "issues": ["short, specific problems"]}}"""
    out = subprocess.run(["claude", "-p", prompt, "--model", "claude-opus-5-5"], capture_output=True, text=True, timeout=300).stdout
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0))
    except (AttributeError, ValueError):
        return {"score": 0, "issues": [f"judge gave no verdict: {out[:200]}"]}


results = {}
for name, (said, bar) in SCENARIOS.items():
    if only and name not in only:
        continue
    transcript = call(name, said)
    verdict = judge(name, bar, transcript)
    results[name] = {"score": verdict.get("score"), "latency": latency(transcript), "issues": verdict.get("issues", []),
                     "transcript": transcript}
    print(f"{name:13} {verdict.get('score')}/5  replies after {results[name]['latency']}s  {'; '.join(verdict.get('issues', []))[:300]}", flush=True)
json.dump(results, open(os.path.join(HERE, f"results-{backend}.json"), "w"), indent=1)
scores = [r["score"] or 0 for r in results.values()]
print(f"{backend}: {sum(scores) / max(1, len(scores)):.1f}/5 average")
