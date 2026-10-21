#!/usr/bin/env python3
"""openlogz: pull OpenRouter Broadcast traces from a Hugging Face bucket and
rebuild them into readable conversations.

    python openlogz.py sync    --bucket OWNER/NAME [--prefix PATH] [--data DIR]
    python openlogz.py inspect [--data DIR]
    python openlogz.py import  EXPORT.json [--bucket OWNER/NAME]
    python openlogz.py build   [--data DIR] [--out DIR]

`sync` needs `huggingface_hub` and a read token (HF_TOKEN or `hf auth login`).
`inspect` and `build` use only the standard library.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "viewer" / "template.html"
DATA_PLACEHOLDER = "/*__OPENLOGZ_DATA__*/null"


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> None:
    try:
        from huggingface_hub import sync_bucket
    except ImportError:
        sys.exit("sync needs huggingface_hub: pip install -U huggingface_hub")
    src = f"hf://buckets/{args.bucket.strip('/')}"
    if args.prefix:
        src += "/" + args.prefix.strip("/")
    dest = Path(args.data) / "raw"
    dest.mkdir(parents=True, exist_ok=True)
    print(f"syncing {src} -> {dest}")
    sync_bucket(src, str(dest))
    n = sum(1 for _ in dest.rglob("*.json"))
    print(f"{n} json files under {dest}")


# --------------------------------------------------------------------------
# reading trace files
# --------------------------------------------------------------------------

EXPORT_FORMAT = "openrouter-chat"  # the chat app's Export file


def iter_trace_files(data_dir: Path):
    raw = data_dir / "raw"
    root = raw if raw.exists() else data_dir
    for p in sorted(root.rglob("*.json")):
        if p.name.startswith(".openrouter-connection-test"):
            continue
        yield p


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"skip {path}: {e}", file=sys.stderr)
        return None


def load_traces(path: Path, doc=None) -> list[dict]:
    """A file holds {"trace": {...}} per the docs; tolerate lists/bare traces."""
    doc = read_json(path) if doc is None else doc
    if isinstance(doc, dict) and doc.get("format") == EXPORT_FORMAT:
        return []
    if isinstance(doc, list):
        return [t for t in doc if isinstance(t, dict)]
    if isinstance(doc, dict):
        if isinstance(doc.get("traces"), list):
            return [t for t in doc["traces"] if isinstance(t, dict)]
        if isinstance(doc.get("trace"), dict):
            t = dict(doc["trace"])
            t.setdefault("_exported_at", doc.get("exported_at"))
            return [t]
        return [doc]
    return []


def first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def text_of(content) -> str:
    """Flatten OpenAI-style content (str | [{type,text}] | dict) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if "text" in part:
                    parts.append(str(part["text"]))
                elif part.get("type") in ("image_url", "input_image"):
                    parts.append("[image]")
        return "\n".join(parts)
    if isinstance(content, dict):
        return text_of(first(content, "text", "content", default=""))
    return str(content)


def as_message(m) -> dict | None:
    if isinstance(m, str):
        return {"role": "assistant", "content": m}
    if not isinstance(m, dict):
        return None
    if "message" in m and isinstance(m["message"], dict):  # a "choice"
        return as_message(m["message"])
    role = m.get("role") or "assistant"
    msg = {"role": role, "content": text_of(m.get("content"))}
    reasoning = first(m, "reasoning", "reasoning_content")
    if reasoning:
        msg["reasoning"] = text_of(reasoning)
    if m.get("name"):
        msg["name"] = m["name"]
    return msg


def messages_of(value) -> list[dict]:
    """Normalise an observation's input or output into a list of messages."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return messages_of(json.loads(value))
        except json.JSONDecodeError:
            return [{"role": "assistant", "content": value}]
    if isinstance(value, list):
        return [m for m in (as_message(v) for v in value) if m]
    if isinstance(value, dict):
        for key in ("messages", "choices", "completion", "output"):
            if key in value:
                return messages_of(value[key])
        if "role" in value or "content" in value:
            m = as_message(value)
            return [m] if m else []
        if "prompt" in value:
            return [{"role": "user", "content": text_of(value["prompt"])}]
    return []


def number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def usage_of(obs: dict) -> dict:
    u = first(obs, "usage", "usageDetails", "usage_details", default={}) or {}
    inp = first(u, "input", "promptTokens", "prompt_tokens", "input_tokens")
    out = first(u, "output", "completionTokens", "completion_tokens", "output_tokens")
    tot = first(u, "total", "totalTokens", "total_tokens")
    cost = first(obs, "calculatedTotalCost", "totalCost", "cost")
    if cost is None:
        c = first(obs, "costDetails", "cost_details", default={}) or {}
        cost = first(c, "total", "totalCost")
    cost = cost if cost is not None else first(u, "cost", "totalCost")
    out_d = {
        "in": number(inp),
        "out": number(out),
        "total": number(tot),
        "cost": number(cost),
    }
    if out_d["total"] is None and (out_d["in"] or out_d["out"]):
        out_d["total"] = (out_d["in"] or 0) + (out_d["out"] or 0)
    return out_d


def generations(trace: dict) -> list[dict]:
    """Flatten one trace into the LLM generations it contains."""
    obs_list = first(trace, "observations", "spans", "generations", default=[]) or []
    gens = []
    for obs in obs_list:
        if not isinstance(obs, dict):
            continue
        kind = str(obs.get("type", "")).upper()
        if kind and kind != "GENERATION" and "model" not in obs:
            continue
        gens.append(obs)
    if not gens and ("input" in trace or "output" in trace):
        gens = [trace]  # trace-level input/output only
    out = []
    for i, obs in enumerate(gens):
        meta = obs.get("metadata") or {}
        out.append({
            "id": str(first(obs, "id", default=f"{trace.get('id', '?')}:{i}")),
            "trace_id": str(trace.get("id", "")),
            "ts": first(obs, "startTime", "start_time", "timestamp",
                        default=first(trace, "timestamp", "_exported_at", default="")),
            "end": first(obs, "endTime", "end_time", default=None),
            "model": first(obs, "model", default=first(meta, "model", default="")),
            "session": first(trace, "sessionId", "session_id",
                             default=first(meta, "session_id", "sessionId")),
            "user": first(trace, "userId", "user_id", default=None),
            "name": first(trace, "name", default=None),
            "input": messages_of(obs.get("input")),
            "output": messages_of(obs.get("output")),
            "usage": usage_of(obs),
        })
    return out


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def shape(v, depth=0, max_depth=4):
    """Compact structural summary of a JSON value (keys and types, no text)."""
    if depth >= max_depth:
        return type(v).__name__
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0], depth + 1)] if v else []
    if isinstance(v, str):
        return f"str[{len(v)}]"
    return type(v).__name__


def cmd_inspect(args: argparse.Namespace) -> None:
    data = Path(args.data)
    files = list(iter_trace_files(data))
    if not files:
        sys.exit(f"no trace files under {data} (run sync first)")
    print(f"{len(files)} trace files; shape of {files[-1].name}:\n")
    print(json.dumps(shape(json.loads(files[-1].read_text("utf-8"))), indent=2))
    gens = [g for f in files for t in load_traces(f) for g in generations(t)]
    print(f"\nparsed {len(gens)} generations")
    print("models:", dict(Counter(g["model"] for g in gens).most_common(8)))
    print("with session id:", sum(1 for g in gens if g["session"]))
    print("empty input:", sum(1 for g in gens if not g["input"]),
          " empty output:", sum(1 for g in gens if not g["output"]))


# --------------------------------------------------------------------------
# build: generations -> threads
# --------------------------------------------------------------------------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def mhash(m: dict) -> str:
    return hashlib.sha1(f"{m['role']}\x00{norm(m['content'])}".encode()).hexdigest()[:16]


def ihash(msgs: list[dict]) -> str:
    return hashlib.sha1("|".join(mhash(m) for m in msgs).encode()).hexdigest()[:16]


def reply_of(gen: dict) -> dict:
    outs = [m for m in gen["output"] if m["role"] == "assistant"] or gen["output"]
    msg = dict(outs[-1]) if outs else {"role": "assistant", "content": ""}
    msg["role"] = "assistant"
    msg.update(ts=gen["ts"], model=gen["model"], gen=gen["id"], usage=gen["usage"])
    return msg


class Threader:
    """Reassemble chat threads from stateless requests.

    Every request re-sends the history, so a request that contains an
    assistant message we have seen continues the thread that produced it.
    An identical input seen twice is a regeneration ("swipe"): the new reply
    is stored as a variant of the same turn. Continuing from a turn that is
    not the thread's latest makes a fork.
    """

    def __init__(self) -> None:
        self.threads: list[dict] = []
        self.by_reply: dict[str, tuple[int, int, int]] = {}  # hash -> thread, turn, variant
        self.by_input: dict[str, tuple[int, int]] = {}       # input hash -> thread, turn

    def _new_thread(self, system: str, turns: list[dict], forked=None) -> int:
        self.threads.append({"system": system, "turns": turns, "forked_from": forked})
        return len(self.threads) - 1

    def _turn(self, m: dict) -> dict:
        return {"role": m["role"], "variants": [m], "chosen": 0}

    def seed(self, chat: dict) -> None:
        """Start from a chat the app exported, so traces can continue it."""
        turns = [self._turn(m) for m in chat["messages"]]
        ti = self._new_thread("", turns)
        self.threads[ti].update(title=chat.get("title"), chat_id=chat.get("id"),
                                created=chat.get("created"), last=chat.get("updated"))
        self._index_tail(ti, 0)

    def _add_variant(self, ti: int, i: int, reply: dict, key: str) -> None:
        turn = self.threads[ti]["turns"][i]
        h = mhash(reply)
        for v in turn["variants"]:
            if mhash(v) == h:  # the same reply seen twice (export + trace)
                for k in ("ts", "gen", "usage", "reasoning", "model"):
                    if reply.get(k) and not v.get(k):
                        v[k] = reply[k]
                break
        else:
            turn["variants"].append(reply)
            self.by_reply[h] = (ti, i, len(turn["variants"]) - 1)
        self.by_input[key] = (ti, i)

    def _index_tail(self, ti: int, start: int) -> None:
        for i, turn in enumerate(self.threads[ti]["turns"][start:], start):
            for k, v in enumerate(turn["variants"]):
                if v["role"] == "assistant":
                    self.by_reply[mhash(v)] = (ti, i, k)

    def add(self, gen: dict) -> None:
        msgs = gen["input"]
        system = "\n\n".join(m["content"] for m in msgs if m["role"] == "system")
        convo = [m for m in msgs if m["role"] != "system"]
        reply = reply_of(gen)

        # 1. same input as before -> regeneration of that turn
        key = ihash(msgs)
        if key in self.by_input:
            self._add_variant(*self.by_input[key], reply, key)
            return

        # 2. continues an assistant reply we already have
        for j in range(len(convo) - 1, -1, -1):
            if convo[j]["role"] != "assistant":
                continue
            hit = self.by_reply.get(mhash(convo[j]))
            if not hit:
                continue
            ti, i, k = hit
            th = self.threads[ti]
            th["turns"][i]["chosen"] = k
            new_msgs = convo[j + 1:]
            # same messages since that reply, then a reply we have: a swipe
            # whose input differs only outside the chat (e.g. system prompt)
            after = th["turns"][i + 1:i + 1 + len(new_msgs)]
            nxt = i + 1 + len(new_msgs)
            if (nxt < len(th["turns"]) and th["turns"][nxt]["role"] == "assistant"
                    and [mhash(t["variants"][t["chosen"]]) for t in after]
                    == [mhash(m) for m in new_msgs]):
                self._add_variant(ti, nxt, reply, key)
                return
            if i != len(th["turns"]) - 1:  # branching off an earlier turn
                prefix = copy.deepcopy(th["turns"][: i + 1])
                ti = self._new_thread(th["system"], prefix, {"thread": ti, "turn": i})
                th = self.threads[ti]
            start = len(th["turns"])
            th["turns"].extend(self._turn(m) for m in new_msgs)
            th["turns"].append(self._turn(reply))
            self.by_input[key] = (ti, len(th["turns"]) - 1)
            self._index_tail(ti, start)
            return

        # 3. a thread we have not seen the start of
        turns = [self._turn(m) for m in convo] + [self._turn(reply)]
        ti = self._new_thread(system, turns)
        self.by_input[key] = (ti, len(turns) - 1)
        self._index_tail(ti, 0)


def summarise(th: dict, idx: int) -> dict:
    turns = th["turns"]
    first_user = next((t["variants"][0]["content"] for t in turns if t["role"] == "user"), "")
    title = (th.get("title") or norm(first_user)[:80] or norm(th["system"])[:80]
             or f"thread {idx + 1}")
    times = [v.get("ts") for t in turns for v in t["variants"] if v.get("ts")]
    times += [iso(th[k]) for k in ("created", "last") if th.get(k)]
    replies = [v for t in turns for v in t["variants"] if v.get("gen") or v.get("usage")]
    models = Counter(v.get("model") for v in replies if v.get("model"))
    tok = sum((v["usage"].get("total") or 0) for v in replies)
    cost = sum((v["usage"].get("cost") or 0) for v in replies)
    words = sum(len(t["variants"][t["chosen"]]["content"].split()) for t in turns)
    return {
        "id": f"t{idx + 1}",
        "title": title,
        "started": min(times) if times else "",
        "updated": max(times) if times else "",
        "models": [m for m, _ in models.most_common()],
        "turns_count": len(turns),
        "generations": len(replies),
        "tokens": int(tok),
        "cost": round(cost, 6),
        "words": words,
        **th,
    }


def to_markdown(t: dict) -> str:
    lines = [f"# {t['title']}", "",
             f"*{t['started']} → {t['updated']} · {', '.join(t['models'])} · "
             f"{t['turns_count']} turns*", ""]
    if t["system"]:
        lines += ["<details><summary>System prompt</summary>", "", t["system"], "",
                  "</details>", ""]
    for turn in t["turns"]:
        v = turn["variants"][turn["chosen"]]
        who = "User" if turn["role"] == "user" else "AI"
        lines += [f"### {who}", "", v["content"], ""]
    return "\n".join(lines)


def iso(v) -> str:
    """Epoch milliseconds (the app's dates) or a string -> ISO 8601."""
    if isinstance(v, (int, float)):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(v / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    return str(v or "")


def export_chats(doc: dict) -> list[dict]:
    """Chats from the app's Export file, as {id, title, ..., messages}."""
    chats = []
    for c in doc.get("chats") or []:
        msgs = []
        for m in c.get("messages") or []:
            if m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
                continue
            msg = {"role": m["role"], "content": m["content"]}
            if m["role"] == "assistant":
                msg.update(model=m.get("model") or "", provider=m.get("provider"),
                           usage=usage_of({"usage": m.get("usage") or {}}))
            msgs.append(msg)
        if msgs:
            chats.append({**{k: c.get(k) for k in ("id", "title", "created", "updated")},
                          "messages": msgs, "_exported": doc.get("exportedAt", "")})
    return chats


def load_dir(data_dir: Path, cache: dict | None = None):
    """Parse every file under data_dir into (generations, exported chats).

    `cache` maps path -> (mtime, size, gens, chats) so a long-running server
    only re-reads files that changed."""
    gens, chats = [], []
    for f in iter_trace_files(data_dir):
        try:
            st = f.stat()
        except OSError:
            continue
        hit = cache.get(str(f)) if cache is not None else None
        if hit and hit[:2] == (st.st_mtime, st.st_size):
            g, c = hit[2], hit[3]
        else:
            doc = read_json(f)
            if isinstance(doc, dict) and doc.get("format") == EXPORT_FORMAT:
                g, c = [], export_chats(doc)
            else:
                g, c = [x for t in load_traces(f, doc) for x in generations(t)], []
            if cache is not None:
                cache[str(f)] = (st.st_mtime, st.st_size, g, c)
        gens += g
        chats += c
    return gens, chats


def build_payload(gens: list[dict], chats: list[dict]) -> dict:
    # Several exports hold the same chat: keep its longest, newest copy.
    best: dict = {}
    for c in chats:
        k = c.get("id") or id(c)
        if k not in best or (len(c["messages"]), c["_exported"]) > (
                len(best[k]["messages"]), best[k]["_exported"]):
            best[k] = c
    th = Threader()
    for c in sorted(best.values(), key=lambda c: c.get("created") or 0):
        th.seed(copy.deepcopy(c))
    for g in sorted(gens, key=lambda g: str(g["ts"])):
        th.add(g)
    threads = [summarise(t, i) for i, t in enumerate(th.threads)]
    for t in threads:
        if t["forked_from"]:
            t["forked_from"]["thread"] = threads[t["forked_from"]["thread"]]["id"]
    threads.sort(key=lambda t: t["updated"], reverse=True)
    return {"generations": len(gens), "imported": len(best), "threads": threads}


def render_html(payload: dict) -> str:
    return TEMPLATE.read_text("utf-8").replace(
        DATA_PLACEHOLDER, json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))


def cmd_build(args: argparse.Namespace) -> None:
    data, out = Path(args.data), Path(args.out)
    gens, chats = load_dir(data)
    if not gens and not chats:
        sys.exit(f"nothing found under {data} (run sync or import first)")
    payload = build_payload(gens, chats)
    threads = payload["threads"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "conversations.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    md_dir = out / "markdown"
    md_dir.mkdir(exist_ok=True)
    for t in threads:
        (md_dir / f"{t['id']}.md").write_text(to_markdown(t), "utf-8")
    (out / "viewer.html").write_text(render_html(payload), "utf-8")
    print(f"{len(gens)} generations + {payload['imported']} imported chats "
          f"-> {len(threads)} threads")
    print(f"open {out / 'viewer.html'}")


def cmd_import(args: argparse.Namespace) -> None:
    """Add an Export file from the chat app (old history from before Broadcast)."""
    src = Path(args.file)
    doc = read_json(src)
    if not isinstance(doc, dict) or doc.get("format") != EXPORT_FORMAT:
        sys.exit(f"{src} is not an OpenRouter Chat export file")
    name = f"imports/{src.name}"
    dest = Path(args.data) / "raw" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    print(f"{len(export_chats(doc))} chats -> {dest}")
    if args.bucket:
        from huggingface_hub import batch_bucket_files
        batch_bucket_files(args.bucket, add=[(str(src), name)])
        print(f"uploaded to hf://buckets/{args.bucket}/{name}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="download traces from the bucket")
    s.add_argument("--bucket", required=True, help="OWNER/NAME")
    s.add_argument("--prefix", default="", help="folder inside the bucket")
    s.add_argument("--data", default="data")
    i = sub.add_parser("inspect", help="show the structure of downloaded traces")
    i.add_argument("--data", default="data")
    b = sub.add_parser("build", help="rebuild conversations and the viewer")
    b.add_argument("--data", default="data")
    b.add_argument("--out", default="out")
    m = sub.add_parser("import", help="add the chat app's Export file (old history)")
    m.add_argument("file")
    m.add_argument("--data", default="data")
    m.add_argument("--bucket", default="", help="also upload to OWNER/NAME")
    args = ap.parse_args(argv)
    {"sync": cmd_sync, "inspect": cmd_inspect, "build": cmd_build,
     "import": cmd_import}[args.cmd](args)


if __name__ == "__main__":
    main()
