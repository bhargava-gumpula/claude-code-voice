"""The voice's task list: several requests in a row, each outcome told once, in order, never over speech."""
import os, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
from jobs import Jobs  # noqa: E402

asks, told = [], []
quiet = threading.Event()
quiet.set()
jobs = Jobs(lambda **m: asks.append(m["ask"]), told.append, quiet.is_set, wait=0.5)


def until(cond, secs=5):
    end = time.time() + secs
    while not cond() and time.time() < end:
        time.sleep(0.05)
    return cond()


# a quick task answers inline and is never announced again
t = threading.Thread(target=lambda: told.append("inline:" + jobs.work("say hi")))
t.start()
assert until(lambda: len(asks) == 1)
jobs.finished(asks[0]["id"], "hi there")
t.join()
assert told == ["inline:Done. Result:\nhi there"], told
time.sleep(0.6)
assert len(told) == 1, told

# task 1 then task 2 straight after: both go to Claude, the second is queued behind the first
told.clear(); asks.clear()
assert jobs.work("run the tests").startswith("Still going")
assert jobs.work("update the README").startswith("Next up")
assert [a["task"] for a in asks] == ["run the tests", "update the README"]
assert 'Working on "run the tests"' in jobs.check() and 'Then "update the README"' in jobs.check()

# task 1 finishes while the user is talking: it waits, then is told on its own, naming the task
quiet.clear()
jobs.finished(asks[0]["id"], "12 passed")
time.sleep(0.8)
assert told == [], told
quiet.set()
assert until(lambda: len(told) == 1)
assert 'Finished "run the tests"' in told[0] and "12 passed" in told[0] and '"update the README"' in told[0], told[0]

# task 2 finishes later: told once, on its own
jobs.finished(asks[1]["id"], "README updated")
assert until(lambda: len(told) == 2)
assert 'Finished "update the README"' in told[1] and "run the tests" not in told[1], told[1]
time.sleep(2)
assert len(told) == 2, told

# two outcomes that land together are told together, in the order asked
told.clear(); asks.clear()
jobs.work("a"); jobs.work("b")
quiet.clear()
jobs.finished(asks[1]["id"], "B"); jobs.finished(asks[0]["id"], "A")
quiet.set()
assert until(lambda: len(told) == 1)
assert told[0].index('"a"') < told[0].index('"b"') and "one by one" in told[0], told[0]

# a result for a task asked before a reconnect is still told
told.clear()
jobs.finished("unknown-id", "deployed")
assert until(lambda: len(told) == 1) and "deployed" in told[0]
assert jobs.check() == "Nothing is being worked on right now."

# the talker restating a request it already made does not run it twice
asks.clear()
jobs.work("In the project folder, count the commits on branch main and report the number.")
assert jobs.work("Count the commits on branch main.").startswith("Already on it")
assert jobs.work("Count the commits on main, then list the functions in calc.py.").startswith("Not started")
assert jobs.work("list the functions in calc.py").startswith("Next up") and len(asks) == 2
print("ok")
