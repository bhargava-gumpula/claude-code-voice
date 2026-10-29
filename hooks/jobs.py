"""The voice's task list, shared by both backends.

Every request is taken: it goes to Claude Code (which runs them one after another, in the order asked) and its
outcome comes back as its own [done] message naming the task. Outcomes are announced one at a time, never over
the voice's own speech or the user's, and outcomes that land together are told together.
"""
import threading, time, uuid


class Jobs:
    def __init__(self, emit, announce, idle, wait=6.0):
        """emit: the stdout protocol. announce(text): put a [done] message into the conversation.
        idle(): True when nobody is speaking (the voice is not playing and the user is not talking)."""
        self.emit, self.announce, self.idle, self.wait = emit, announce, idle, wait
        self.jobs = {}  # id -> {"task", "result", "done": Event, "told", "progress"}; insertion order = run order
        self.lock = threading.Lock()
        self.ready = threading.Condition(self.lock)
        threading.Thread(target=self._announcer, daemon=True).start()

    def open(self):
        return [j for j in self.jobs.values() if not j["done"].is_set()]

    def work(self, task):
        task = (task or "").strip()
        if not task:
            return "No instruction given."
        with self.lock:
            ahead = len(self.open())
            jid = uuid.uuid4().hex
            job = self.jobs[jid] = {"task": task, "result": None, "done": threading.Event(), "told": False, "progress": ""}
        self.emit(ask={"id": jid, "task": task})
        if ahead:
            return (f"Queued: it starts after the {ahead} thing{'s' if ahead > 1 else ''} already in progress. "
                    "Tell the user in a few words that it's next. Each outcome arrives as its own [done] message.")
        if job["done"].wait(self.wait):
            with self.lock:
                job["told"] = True
            return f"Done. Result:\n{job['result']}"
        return ("Still going; it takes longer than a few seconds. Tell the user briefly and keep talking. "
                "The outcome arrives as a [done] message.")

    def progress(self, text, jid=None):
        with self.lock:
            job = self.jobs.get(jid) or next(iter(self.open()), None)
            if job:
                job["progress"] = text

    def check(self):
        with self.lock:
            lines, told = [], []
            for j in self.jobs.values():
                if j["done"].is_set() and not j["told"]:
                    j["told"] = True
                    told.append(f'Finished "{j["task"]}": {j["result"][:1500]}')
            running = self.open()
            for i, j in enumerate(running):
                lines.append(f'{"Working on" if i == 0 else "Then"} "{j["task"]}"' + (f" (right now: {j['progress']})" if i == 0 and j["progress"] else ""))
        if not lines and not told:
            return "Nothing is being worked on right now."
        return "\n".join(told + lines)

    def finished(self, jid, text):
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:  # asked before a reconnect: still worth telling
                job = self.jobs[jid] = {"task": "something you asked for earlier", "result": None,
                                        "done": threading.Event(), "told": False, "progress": ""}
            job["result"] = text
            job["done"].set()
            self.ready.notify()

    def _announcer(self):
        while True:
            with self.lock:
                while not any(j["done"].is_set() and not j["told"] for j in self.jobs.values()):
                    self.ready.wait()
            time.sleep(0.3)  # a work() call waiting on this result may still claim it
            while not self.idle():
                time.sleep(0.2)
            with self.lock:
                news = [j for j in self.jobs.values() if j["done"].is_set() and not j["told"]]
                for j in news:
                    j["told"] = True
                left = [j["task"] for j in self.open()]
                for jid in [k for k, j in self.jobs.items() if j["told"]]:
                    del self.jobs[jid]
            if not news:
                continue
            msg = "\n".join(f'[done] Finished "{j["task"]}". Outcome: {j["result"][:3000 // len(news)]}' for j in news)
            msg += ("\nStill in progress: " + "; ".join(f'"{t}"' for t in left) + "." if left else "")
            msg += "\nTell the user what happened in your own words, briefly" + (", one by one." if len(news) > 1 else ".")
            self.announce(msg)
            time.sleep(1.5)  # let the reply to this one start before the next can go out
