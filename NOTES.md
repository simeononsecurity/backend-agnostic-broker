# Project Notes — backend-agnostic-broker

## Repository
- **Selected path:** `~/Documents/GitHub/backend-agnostic-broker`
- **Decision:** No existing repository under `~/Documents/GitHub` matched a
  backend-agnostic AI broker (searched for `*broker*`, `*strata*`, `*ollama*`,
  `*vllm*`, `*llama*`, `*gateway*`, `*router*` — none found). Created a fresh
  repository per the discovery rules.
- **Git:** initialized locally; commits are **local-only** per task constraints.
  Nothing is pushed, no releases, no external GitHub settings changed.

## Environment (host)
- macOS 26.5.2, Docker 28.3.0, git 2.50.1
- Python 3.13.7 (via Homebrew) used for the project `.venv` (host `python3` is 3.9.6)
- Node v26.10.0 available; Go and Rust **not** installed on host.

## Language decision
- **Python 3.11+** — see `docs/adr/0001-language-choice.md`.

## Acceptance-gate evidence index
See `docs/EVIDENCE.md` for the running ledger mapping each of the 22 gates to
concrete command output / file paths.
