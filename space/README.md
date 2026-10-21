---
title: OpenLogz
emoji: 📖
colorFrom: purple
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Read OpenRouter role-play logs like a book
---

# OpenLogz

Reads OpenRouter Broadcast traces and OpenRouter Chat exports from a Storage Bucket
and shows them as conversations, either as a chat or as a book.

Source: https://github.com/SoulFireMage/openlogz. This Space builds from a pinned
commit of that repository (see `REF` in the `Dockerfile`).

## Settings

- **Bucket:** attach `SoulFireMage/openrouter-rp-logs` at `/data` (Settings → Storage
  Buckets).
- **Sharing:** with a protected Space, share https://soulfiremage-openlogz.hf.space
  (the Hub page 404s for others). Optional `APP_PASSWORD` secret adds a login.
