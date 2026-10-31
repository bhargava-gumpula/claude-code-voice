"""Interrupting the ElevenLabs voice: the mic gate lets the user through and keeps the voice's own echo out."""
import os, random, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
os.environ.setdefault("ELEVENLABS_API_KEY", "test")
import agent  # noqa: E402

random.seed(1)
# speakers loud at the mic: echo ~30% of what plays; the voice plays at ~0.4
gate = agent.EchoGate()
for _ in range(40):  # it talks, nobody interrupts: echo alone never gets through
    out = random.uniform(0.3, 0.5)
    assert not gate.passes(out * random.uniform(0.25, 0.35), out)
print(f"learned echo ratio {gate.echo():.2f}")
assert gate.passes(0.32 + 0.4 * 0.3, 0.4), "normal speech over the voice must get through"
assert not gate.passes(0.4 * 0.35, 0.4), "a loud echo peak must not"

# headphones: no echo at all, so even quiet speech interrupts
gate = agent.EchoGate()
for _ in range(20):
    assert not gate.passes(0.0, 0.4)
assert gate.passes(0.1, 0.4)

# the old fixed gate (0.3) blocked ordinary speech at ~0.2 with no echo; this one does not
assert agent.EchoGate().passes(0.2, 0.0)
print("ok")
