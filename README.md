# openlogz

Send OpenRouter request logs (prompts and completions) to a private Hugging Face
Storage Bucket, then rebuild them into readable role-play conversations.

```
OpenRouter ──Broadcast──▶ HF bucket ──sync──▶ data/raw/*.json ──build──▶ out/viewer.html
 (one key)   (1 JSON file                       (local copy)          out/markdown/*.md
             per request)                                             out/conversations.json
```

## Stage 1: get traces into the bucket

### 1. Create a private bucket

Go to <https://huggingface.co/new-bucket>. Set owner to `SoulFireMage`, pick a name
(for example `openrouter-rp-logs`), and set it to **Private**. The logs are private
role-play text, so keep the bucket private.

Or from the CLI: `hf buckets create openrouter-rp-logs --private`

### 2. Make S3 credentials

1. Go to <https://huggingface.co/settings/tokens> and create a **fine-grained** token
   with write access to your own namespace (a classic Write token also works).
2. In that token's `⋮` menu, choose **Generate S3 credentials**.
3. Copy the **Access Key ID** (it starts with `HFAK`) and the **Secret Access Key**.
   The secret is shown once.

### 3. Add the destination in OpenRouter

Go to OpenRouter → Settings → Observability, turn on "Send traces to Langfuse, Datadog,
LangSmith, and more", then add **Hugging Face Storage Buckets** with these settings:

| Field | Value |
|---|---|
| Name | anything, e.g. `RP logs` |
| Namespace | `SoulFireMage` |
| Bucket Name | `openrouter-rp-logs` (the bucket name only, without the namespace) |
| Access Key Id / Secret | from step 2 |
| Path Template | `{apiKeyName}/{date}` (puts each key in its own folder) |
| API key filter | **only** the role-play key. Without this, every key's traffic goes to the bucket. |
| Sampling rate | `1.0` (every request) |
| Privacy Mode | **off**. If it is on, prompt and completion text is left out. |

Click **Test Connection**. OpenRouter writes `.openrouter-connection-test.json` to the
bucket, and the destination only saves if that write works. A 401 or 403 means a bad
key, a token without write access, or a wrong namespace or bucket name.

Then send one message in the role-play and check
`https://huggingface.co/buckets/SoulFireMage/openrouter-rp-logs`. You should see one
`{traceId}-{timestamp}.json` file per request.

> **Older chats are not sent.** Broadcast only sends requests made after the
> destination is set up. OpenRouter's docs say nothing about backfilling earlier
> requests, so assume the 78M tokens already used will not show up. The best copy of
> that history is probably your chat interface's own database or export. An
> `import` command for that is a natural next step.

## Stage 2: read the logs

```bash
pip install -U huggingface_hub
hf auth login                      # or export HF_TOKEN=... (read access is enough)

python openlogz.py sync --bucket SoulFireMage/openrouter-rp-logs
python openlogz.py inspect         # shows the real JSON shape and what was parsed
python openlogz.py build           # writes out/viewer.html
```

Open `out/viewer.html` in a browser. It is one offline file with the data built in,
so nothing leaves your machine. Don't put it anywhere public: it contains the full
logs.

### How conversations are rebuilt

Each request carries the whole chat history, so 500 turns produce 500 files that
mostly repeat each other. `build` turns them back into conversations:

- **Thread:** a request whose history contains an AI reply we already have continues
  that conversation, and only the new messages are added.
- **Swipe / regenerate:** the same history sent again gives a second reply to the
  same turn. The viewer shows these as `‹ 2/3 ›`, and the reply the story actually
  went on with is chosen by default. Changes to the system prompt (lorebook, memory
  injection) don't break this.
- **Fork:** carrying on from an older turn makes a new thread that links back to
  where it split.
- DeepSeek's `reasoning` text is kept and shown as a collapsible block.

The viewer has full-text search across all threads, a jump to the latest turn,
markdown export, and light/dark themes, and it works on a phone.

### Caveat: field names are a best guess

OpenRouter's docs describe the files (a top-level `trace` object plus `exported_at`,
full input/output messages, tokens, cost, timing) but don't publish the full schema.
The parser accepts the common variations (`messages`/`choices`, string or content-part
content, Langfuse-style `observations`). When real files arrive, run `inspect`. If it
reports empty inputs or outputs, the field mapping in `generations()` in
`openlogz.py` needs a small change.

## Possible next steps

- `import` from the chat interface's own history to recover pre-broadcast turns.
- Continuous sync: an HF Job triggered by a bucket webhook, or a cron job, that runs
  `sync && build`.
- A private HF Space that shows the viewer straight from the bucket, readable from a
  phone without a local build.
- Analysis: words per day, character/name frequency, model changes over time, cost
  per scene.
- Story tools: summaries per chapter, a searchable "story bible" (characters, places,
  plot threads) pulled out by an LLM, or semantic search over embeddings.
- Export as a clean dataset (JSONL of chosen turns) for fine-tuning or archiving.

## Tests

`python -m unittest discover tests`
