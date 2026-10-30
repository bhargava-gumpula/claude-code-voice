"""Smallest checks of the local voice's text handling: run with the mod's venv python."""
import importlib.util, os, sys

sys.argv = ["x"]
spec = importlib.util.spec_from_file_location("local", os.path.join(os.path.dirname(__file__), "..", "hooks", "local.py"))
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)

assert local.spoken("It has `server.py`, **tests** and [docs](x.md).") == "It has server dot pie, tests and docs."
assert local.spoken("   ") == ""
assert local.work({"instruction": ""}) == "No instruction given."
assert local.check_progress() == "Nothing is being worked on right now."
assert "[context]" in local.project_context()


class Boom:  # a recognizer that fails like Parakeet does on an empty clip
    def create_stream(self):
        raise RuntimeError("Invalid input shape: {0,128}")


assert local.hear(Boom(), []) == ""  # too short: skipped
assert local.hear(Boom(), [0.1] * 16000) == ""  # recognizer error: skipped, call stays up

# stutters and fillers: what the user meant
c = local.clean
assert c("Um, can you, can you run the the tests?") == "can you run the tests?", c("Um, can you, can you run the the tests?")
assert c("I- I want to, uh, t- tell you something.") == "I want to, tell you something.", c("I- I want to, uh, t- tell you something.")
assert c("Uh, what's in calc.py?") == "what's in calc.py?", c("Uh, what's in calc.py?")
assert c("that that is fine") == "that is fine"
assert c("It's a bye bye function.") == "It's a bye function."  # ponytail: real repeats get folded too; harmless for the talker
# does it sound finished?
f = local.finished_sentence
assert f("Can you run the tests?") and f("Thanks.")
assert not f("Can you check the.") and not f("so I was thinking and") and not f("Run the tests and uh.") and not f("hmm")
# a pause mid-sentence: the clips join, and an answer to the first half is taken back
local.state["asked"] = False
local.ear.update(text="", last=None)
local.heard("Can you check the")
assert local.ear["text"] == "Can you check the" and local.ear["due"] - local.time.time() > 1
local.ear.update(text="", last={"text": "Can you check the", "at": local.time.time()})
local.heard("readme for typos?")
assert local.ear["text"] == "Can you check the readme for typos?" and local.ear["replaces"] == "Can you check the"
# once work went out for it, a new clip is a new request
local.state["asked"] = True
local.ear.update(text="", last={"text": "Run the tests.", "at": local.time.time()})
local.ear.pop("replaces", None)
local.heard("Also fix the lint.")
assert local.ear["text"] == "Also fix the lint." and "replaces" not in local.ear
# "can you see my files?" is answered, not worked on; naming a thing keeps it a request
assert local.CAPABILITY.match("can you see my files?") and local.CAPABILITY.match("do you have access to my terminal?")
assert not local.CAPABILITY.match("can you read calc.py?") and not local.CAPABILITY.match("can you run the tests?")
assert local.spoken("[done] All good.") == "All good."
assert local.spoken("It's in calc.py and README.md.") == "It's in calc dot pie and README dot M D.", local.spoken("It's in calc.py and README.md.")
# Kokoro drops digits: numbers are spoken as words
w = local.spoken
assert w("There are 2 commits.") == "There are two commits.", w("There are 2 commits.")
assert w("12 passed, 1 failed, 98% done, took 3.5 seconds") == "twelve passed, one failed, ninety-eight percent done, took three point five seconds"
assert w("1,234 lines") == "one thousand two hundred thirty-four lines", w("1,234 lines")
print("ok")
