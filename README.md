# lchat

A small agentic web chat for a local llama.cpp model, with tools that act on your machine.
It was built on a Steam Deck (SteamOS, KDE, Vulkan) around **LFM3-VL-2B**.

- **Chat page**: streaming, multi-turn tool calls, image input, and approval prompts for risky tools.
- **Tools**:
  - `bash`, `read_file`, `write_file`
  - `web_search`: Google in English, several queries in parallel, image links
  - `web_fetch`: a stealth browser
  - `download`: images are shown to the model; PDFs and text are extracted
  - `list_windows`, `window_action`, `screenshot`: KDE/KWin
  - `recall`: search past chats
- **Memory**: chat history in SQLite, searched with EmbeddingGemma-2 embeddings and reranked by Qwen3-Reranker-0.6B.

## Install

```sh
git clone git@github.com:alexcmd/lchat.git && cd lchat
./install.sh --with-llama     # scripts, Python deps, llama.cpp (Vulkan x64) if missing, models
lchat-run                     # starts the 3 model servers + the web chat, prints the URL to open
```

`install.sh` options:
- `--link`: symlink the scripts to this checkout instead of copying them.
- `--no-models`: skip the model download.
- `--with-llama`: fetch prebuilt llama.cpp build `$LLAMA_TAG`, default `b12290`.

You can also run everything straight from the checkout: `./bin/lchat-run`.

## Pieces

| Script | What it does |
|---|---|
| `bin/lchat-run` | `start` (default) / `stop` / `status` / `restart` the stack. A service already answering on its port is reused. `stop` only stops what `lchat-run` started. Logs are in `~/.local/state/lchat/run/`. Flags: `--ctx N` (chat context, default 128000), `--port N` (web port), `--no-memory` (skip the embedding and reranker servers). |
| `bin/lchat-models` | `download` / `list` / `path ROLE`. Downloads the GGUFs into `~/.local/share/lchat/models`, or `$LCHAT_MODELS`. A file already in the Hugging Face cache is symlinked instead of downloaded, and an interrupted download resumes. |
| `bin/lchat-web` | The server: serves the page, proxies the llama OpenAI API, and runs the tools. |
| `bin/lchat` | A minimal terminal chat client, with `/img` to attach an image. |
| `web/index.html` | The chat page: the agent loop and context trimming. |
| `vendor/webcrawler/` | The CloakBrowser crawler used by `web_search` and `web_fetch`. A copy at `~/.agents/skills/webcrawler` takes priority when present. |
| `eval/` | Retrieval-quality tests for `recall` (see below). |

## Models

| Role | Port | Model | Size |
|---|---|---|---|
| chat | 9931 | LiquidAI/LFM3-VL-2B-GGUF `LFM3-VL-2B-QAD-Q4_0` + `mmproj-…-Q8_0` | 1.2 GB (1.0 + 0.2) |
| embed | 9951 | unsloth/embeddinggemma-2-GGUF `Q8_0` | 0.3 GB |
| rerank | 9952 | ggml-org/Qwen3-Reranker-0.6B `Q8_0` | 0.6 GB |

LFM3 is a hybrid model: it keeps KV cache on only 11 of its 24 layers, about 12 KiB per token. So a 128k context needs only about 1.5 GB of KV cache.

## Security

- The tools need a per-launch token. The token is never served over HTTP: it lives only in the URL fragment, written to `~/.local/state/lchat/url` with mode 600.
- Host and Origin checks block DNS rebinding and cross-site requests.
- Only an exact allowlist of llama endpoints is proxied.
- Tools only run when the server listens on loopback, unless you pass `--allow-remote-tools`.
- `bash`, `write_file`, closing windows and sensitive `read_file` paths ask for approval in the page first.

## Recall quality (`eval/`)

To reproduce, start the stack first, then:
- `python3 eval/run_eval.py`: 40 chats, 60 questions.
- `python3 eval/gen_hard.py && python3 eval/run_hard.py`: 240 look-alike chats with ports that change over time.

The live setup takes the top 10 embedding hits and reranks them. On the hard set it finds the right chat in the top 6 for every detail, history and current-value question. The current value comes first 65% of the time, at about 2 s per search.

Questions that need facts from two different chats are not solved: a single search almost never returns both. The model has to call `recall` twice.

## Requirements

- Linux x64 and Python 3.10+, plus `curl`.
- `pdftotext` for PDFs.
- KDE Plasma (Wayland) for the `spectacle` and `qdbus` window and screenshot tools.
