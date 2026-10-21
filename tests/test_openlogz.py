"""Synthetic traces in the shape OpenRouter documents ({"trace": {...}} with
GENERATION observations). The exact field names are a best guess until real
files arrive; `openlogz.py inspect` shows the real shape."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openlogz  # noqa: E402

SYS = {"role": "system", "content": "You are Captain Vey of the starship Meridian."}


def trace(n, messages, reply, system=SYS, reasoning=None):
    out = {"role": "assistant", "content": reply}
    if reasoning:
        out["reasoning"] = reasoning
    return {
        "exported_at": f"2026-10-21T10:{n:02d}:30Z",
        "trace": {
            "id": f"tr{n}",
            "timestamp": f"2026-10-21T10:{n:02d}:00Z",
            "observations": [{
                "id": f"gen{n}",
                "type": "GENERATION",
                "model": "deepseek/deepseek-chat-v3",
                "startTime": f"2026-10-21T10:{n:02d}:00Z",
                "input": {"messages": [system, *messages]},
                "output": out,
                "usage": {"input": 100 + n, "output": 50, "total": 150 + n},
                "calculatedTotalCost": 0.001,
            }],
        },
    }


U = lambda s: {"role": "user", "content": s}
A = lambda s: {"role": "assistant", "content": s}


class BuildTest(unittest.TestCase):
    def build(self, traces):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d, "data", "raw", "openrouter-traces", "2026-10-21")
            raw.mkdir(parents=True)
            for i, t in enumerate(traces):
                (raw / f"tr{i}-x.json").write_text(json.dumps(t))
            Path(d, "data", "raw", ".openrouter-connection-test.json").write_text("{}")
            out = Path(d, "out")
            openlogz.main(["build", "--data", str(Path(d, "data")), "--out", str(out)])
            self.assertTrue((out / "viewer.html").exists())
            html = (out / "viewer.html").read_text()
            self.assertNotIn(openlogz.DATA_PLACEHOLDER, html)
            return json.loads((out / "conversations.json").read_text())

    def test_threads_swipes_and_forks(self):
        h1 = [U("*boards the bridge* Status?")]
        r1 = "*turns* \"All systems green.\""
        h2 = h1 + [A(r1), U("Set course for Tau.")]
        traces = [
            trace(1, h1, r1, reasoning="greet"),
            trace(2, h2, "Course laid in."),
            trace(3, h2, "Aye, plotting now."),                      # swipe
            trace(4, h2 + [A("Aye, plotting now."), U("Engage.")], "Engaging."),
            trace(5, h1 + [A(r1), U("Actually, dock first.")], "Docking."),  # fork
            trace(6, [U("New story: a tavern.")], "The tavern is loud."),
            # system prompt changed (e.g. lorebook) but same chat: still a swipe
            trace(7, h2 + [A("Aye, plotting now."), U("Engage.")], "Punching it.",
                  system={"role": "system", "content": "Changed lore"}),
        ]
        data = self.build(traces)
        self.assertEqual(data["generations"], 7)
        threads = {t["title"][:12]: t for t in data["threads"]}
        self.assertEqual(len(data["threads"]), 3)

        main = next(t for t in data["threads"]
                    if not t["forked_from"] and t["title"].startswith("*boards"))
        self.assertEqual([t["role"] for t in main["turns"]],
                         ["user", "assistant", "user", "assistant", "user", "assistant"])
        swipe = main["turns"][3]
        self.assertEqual([v["content"] for v in swipe["variants"]],
                         ["Course laid in.", "Aye, plotting now."])
        self.assertEqual(swipe["chosen"], 1)  # the one the story continued from
        self.assertEqual(len(main["turns"][5]["variants"]), 2)
        self.assertEqual(main["turns"][1]["variants"][0]["reasoning"], "greet")

        fork = next(t for t in data["threads"] if t["forked_from"])
        self.assertEqual(fork["forked_from"], {"thread": main["id"], "turn": 1})
        self.assertEqual(fork["turns"][-1]["variants"][0]["content"], "Docking.")
        self.assertIn("New story: a", threads)

    def test_content_parts_and_choices(self):
        t = trace(1, [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}], "x")
        t["trace"]["observations"][0]["output"] = {
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}]}
        data = self.build([t])
        turns = data["threads"][0]["turns"]
        self.assertEqual(turns[0]["variants"][0]["content"], "Hi")
        self.assertEqual(turns[1]["variants"][0]["content"], "Hello")


if __name__ == "__main__":
    unittest.main()
