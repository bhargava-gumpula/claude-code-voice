#!/usr/bin/env python3
"""Makes a test recording of someone talking, with real pauses, for VOICE_MIC_FILE.

  python test/make_audio.py out.wav "Can you check the|1.8|readme for typos?" ["|20|" "next thing" ...]

Text is spoken with a Kokoro voice unlike the voice's own; a number between bars is that many seconds of
silence. Run with the voice's venv python (it uses the local voice's downloaded models).
"""
import os, re, sys, wave

import numpy as np
import sherpa_onnx as s

MODELS = os.path.expanduser(os.environ.get("VOICE_MODELS", "~/.cache/claude-voice/models"))
k = os.path.join(MODELS, "kokoro-multi-lang-v1_0")
tts = s.OfflineTts(s.OfflineTtsConfig(model=s.OfflineTtsModelConfig(kokoro=s.OfflineTtsKokoroModelConfig(
    model=f"{k}/model.onnx", voices=f"{k}/voices.bin", tokens=f"{k}/tokens.txt", data_dir=f"{k}/espeak-ng-data",
    lexicon=f"{k}/lexicon-us-en.txt", dict_dir=f"{k}/dict"), num_threads=4)))
SPEAKER = int(os.environ.get("TALKER", "16"))  # am_michael: a different voice from the agent's

out = [np.zeros(8000, np.float32)]
for part in re.split(r"\|", "|".join(sys.argv[2:])):
    part = part.strip()
    if not part:
        continue
    if re.fullmatch(r"[\d.]+", part):
        out.append(np.zeros(int(float(part) * 16000), np.float32))
        continue
    a = tts.generate(part, sid=SPEAKER, speed=1.0)
    x = np.asarray(a.samples, np.float32)
    idx = np.linspace(0, len(x) - 1, int(len(x) * 16000 / a.sample_rate)).astype(int)
    out.append(x[idx] * 0.8)
pcm = (np.clip(np.concatenate(out), -1, 1) * 32767).astype(np.int16)
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())
print(f"{sys.argv[1]}: {len(pcm) / 16000:.1f}s")
