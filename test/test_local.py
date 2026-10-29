"""Smallest checks of the local voice's text handling: run with the mod's venv python."""
import importlib.util, os, sys

sys.argv = ["x"]
spec = importlib.util.spec_from_file_location("local", os.path.join(os.path.dirname(__file__), "..", "hooks", "local.py"))
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)

assert local.spoken("It has `server.py`, **tests** and [docs](x.md).") == "It has server.py, tests and docs."
assert local.spoken("   ") == ""
assert local.work({"instruction": ""}) == "No instruction given."
assert local.check_progress() == "Nothing is being worked on right now."
assert "[context]" in local.project_context()


class Boom:  # a recognizer that fails like Parakeet does on an empty clip
    def create_stream(self):
        raise RuntimeError("Invalid input shape: {0,128}")


assert local.hear(Boom(), []) == ""  # too short: skipped
assert local.hear(Boom(), [0.1] * 16000) == ""  # recognizer error: skipped, call stays up
print("ok")
