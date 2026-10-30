"""Plays each recording through the local voice's real ears (VAD + Parakeet), no LLM: what does it hear?"""
import sys, threading, time, wave
sys.path.insert(0, sys.argv[1])
import local as L
vad, asr, tts = L.load_models()
L.player["tts"] = tts
L.state["acked"] = True
got = []
L.heard = lambda said: got.append(said)
L.emit = lambda **k: None
for f in sys.argv[2:]:
    got.clear()
    with wave.open(f) as w: secs = w.getnframes() / 16000
    L.state["live"] = True
    t = threading.Thread(target=L.hear_loop, args=(vad, asr, L.FileMic(f)), daemon=True); t.start()
    time.sleep(secs + 1.5)
    while L.state["transcribing"]: time.sleep(0.1)
    L.state["live"] = False; t.join()
    print(f.rsplit("/", 1)[1], "->", got, flush=True)
