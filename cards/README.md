# Help cards

The knowledge base behind lchat's `linux_help` tool: verified, step-by-step cards for Linux and macOS tasks
and error messages, one JSON file per card. `bin/lchat-cards` loads them into a vector store
(`~/.local/state/lchat/cards.db`, EmbeddingGemma-2 vectors), and the tool searches it with embeddings plus the
Qwen3-Reranker.

| Folder | Cards | Origin |
|---|---|---|
| `linux/`, `posix/`, `macos/`, `authored/` | 1,625 | Hugging Face dataset [`alexcmd/lnxhelp-cards`](https://huggingface.co/datasets/alexcmd/lnxhelp-cards), folder `cards/` (curated snapshot `s2`), revision `800ee88a24bbecd2309e4ad36a72503f35f09f7c` |
| `scenarios/pipelines/` | 15 | lchat: pipelines and text processing (find/xargs, awk/sort/uniq, jq, tee, process substitution) |
| `scenarios/bash-scripts/` | 15 | lchat: complete bash scripts (template with trap, getopts, retry with backoff, flock, parallel jobs, ...) |
| `scenarios/sysadmin/` | 15 | lchat: multi-step admin jobs (rsync snapshots with rotation, systemd user timers, cron + flock, log watch, ...) |
| `scenarios/batch/` | 15 | lchat: batch file work and command chaining (renames with preview, duplicates, archives, git chains, ...) |

`card.schema.json` is the dataset's schema (`schema_version` 2) with two lchat extensions: a step `cmd` may be
up to 1,000 characters (pipelines and chained one-liners), and a card may carry a top-level `script`
(`filename`, `lang`, `content`, `usage`) with a complete script. The dataset cards are unchanged.

## Scenario cards

The 60 `scenarios/` cards were written by Claude Haiku 5.5 workers on 2026-11-12. Every command and script was
run against synthetic files in throwaway directories, except steps that need root, the network or real
services (ssh, systemctl, crontab, package managers, kill), which were syntax-checked only. They have not had a
human review. Their `source.license` is `LicenseRef-lchat-authored`; `source.url` names the reference
documentation.

## Checks

```sh
python3 tools/check_cards.py          # schema, unique ids, `bash -n` of every command and script (needs jsonschema)
lchat-cards sync                      # load into the store; only new or changed cards are re-embedded
lchat-cards search "find files older than 7 days and gzip them"
```

## Updating from the dataset

Copy the dataset's `cards/` folder over the four dataset folders here, update the revision above, run
`tools/check_cards.py`, commit, and re-run `install.sh` (or restart `lchat-web`): the sync re-embeds the
changed cards and drops deleted ones.

## Licences

Licensing is per card: read `source.license`. From the dataset card:

- Arch Wiki based cards: **GFDL-1.3-or-later** (copyleft).
- tldr-pages based cards: **CC-BY-4.0**, credit tldr-pages contributors, https://github.com/tldr-pages/tldr.
- Authored cards: `source.license` is the licence of the reference page each card was checked against, not
  necessarily the licence of the card text. One card refers to Apple copyright material.
- No single licence has been chosen for the dataset.
