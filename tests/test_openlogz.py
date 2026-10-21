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

    def test_export_seeds_threads_that_traces_continue(self):
        export = {"format": "openrouter-chat", "version": 1,
                  "exportedAt": "2026-10-20T00:00:00Z",
                  "chats": [{"id": "c1", "title": "The Meridian", "created": 1760000000000,
                             "updated": 1760900000000, "messages": [
                                 U("Status?"),
                                 {"role": "assistant", "content": "Green.", "model": "deepseek/x",
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                            "cost": 0.5}},
                                 U("Engage."),
                                 A("Engaging.")]}],
                  "profiles": []}
        older = dict(export, exportedAt="2026-10-01T00:00:00Z",
                     chats=[dict(export["chats"][0], messages=export["chats"][0]["messages"][:2])])
        h = [U("Status?"), A("Green."), U("Engage.")]
        traces = [
            older, export,
            trace(1, h, "Engaging."),                       # already in the export
            trace(2, h + [A("Engaging."), U("Faster.")], "Max warp."),
        ]
        data = self.build(traces)
        self.assertEqual(data["imported"], 1)
        self.assertEqual(len(data["threads"]), 1)
        t = data["threads"][0]
        self.assertEqual(t["title"], "The Meridian")
        self.assertEqual([chosen["content"] for chosen in
                          (x["variants"][x["chosen"]] for x in t["turns"])],
                         ["Status?", "Green.", "Engage.", "Engaging.", "Faster.", "Max warp."])
        self.assertEqual(len(t["turns"][3]["variants"]), 1)
        self.assertEqual(t["turns"][3]["variants"][0]["gen"], "gen1")
        self.assertEqual(t["turns"][1]["variants"][0]["usage"]["total"], 12)

    def test_real_broadcast_file(self):
        """A real file from the bucket (user id redacted)."""
        real = json.loads((Path(__file__).parent / "fixtures" /
                           "openrouter_test_trace.json").read_text())
        self.assertEqual(openlogz.generations(real["trace"]), [])  # test button: skipped
        live = json.loads(json.dumps(real))
        live["trace"]["metadata"] = {}
        live["trace"]["tags"] = []
        live["trace"]["apiKeyName"] = "roleplay"
        live["trace"]["sessionId"] = "chat123"
        (g,) = openlogz.generations(live["trace"])
        self.assertEqual(g["input"], [{"role": "user", "content": "What is the capital of France?"}])
        self.assertEqual(g["output"][0]["content"], "The capital of France is Paris.")
        self.assertEqual(g["model"], "openai/gpt-4-turbo")
        self.assertEqual(g["session"], "chat123")
        self.assertEqual(g["usage"], {"in": 50.0, "out": 100.0, "total": 150.0, "cost": 0.02})

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
