# Minerva

A personal AI agent runtime with a governed tool gateway, a tamper-evident audit ledger and
multi-agent job orchestration. Python standard library only.

Author: Jesse Hembrook

Designed and directed by its author and built with Claude, Anthropic's AI model, which wrote
most of the code; the comments keep the dated design calls and the order they were made in.

## Why

An agent that acts on your behalf (reads your mail, spends money on generation APIs, runs
background jobs while you sleep) needs accountability you can check, not trust you extend.
Minerva's answer is that the model never touches the world directly. Every tool call goes
through a gateway process that decides allow, ask or deny, and every decision lands in an
append-only, hash-chained ledger that can be verified and replayed later.

## Architecture

```
     the operator (console or a Discord DM: approvals, verdicts, chat)
                              |
          host loop / supervisor (delivery, chat, the job orchestrator)
                              |
   engines (swappable)        |        orchestrator (bounded thread pool)
   Anthropic API (BYOK)       |          -> one isolated agent session per job
   Hermes via ACP             |
   Venice (OpenAI-compatible) |
                              |  MCP over stdio (JSON-RPC 2.0)
          +-------------------v--------------------+
          | governed tool gateway                  |
          |   GovernanceGate: allow / ask / deny   |
          |   kill switch, spend meter,            |
          |   optional inline warden (second model)|
          +-------------------+--------------------+
                              |  every decision
          hash-chained ledger (append-only JSONL, serialized appends)
```

- **Core** (`src/minerva_core/`): the engine interface and event taxonomy (`engine.py`,
  `events.py`), the ledger, the governance gate, the spend meter, the kill switch, the job queue
  and orchestrator, the agenda, and read-only memory recall.
- **Adapters** (`src/minerva_core/adapters/`): each engine translates its own events into
  one taxonomy, so nothing upstream depends on a provider's shapes. The Anthropic and Venice
  adapters run the agent loop themselves and execute every tool call against Minerva's own
  gateway, which they spawn as a child process. Engines are treated as untrusted (for Hermes,
  once `scripts/configure_minerva.ps1` has switched off its built-in tools; see Known gaps for
  the one place that is not yet true).
- **Gateway** (`src/minerva_core/gateway/`): an MCP server over stdio. Tools register with
  their governance metadata (action class, estimated cost), so the gateway, not the engine,
  owns the policy.
- **Daemons and tools** (`scripts/`): the supervisor, which runs the delivery loop and the job
  orchestrator from one window; chat; the brainstorm tick, the morning brief and the watchdog,
  which run as scheduled tasks; a post-hoc warden; a governance replay tool; and live probes
  for each engine.

## Governance model

Every `tools/call` passes `GovernanceGate` before its handler runs:

- **allow**: the handler runs and the call is ledgered.
- **ask**: the call is fingerprinted (a hash of the canonical tool and arguments), queued for
  the operator's approval, and the engine is told to stand down. An identical call while the
  ask is pending does not queue again. Once the operator approves, the next identical call runs.
- **deny**: refused, and the refusal is ledgered too.

Spending is governed by accumulation, not just per call: the metered lanes record what they
spend (from token usage, a provider's quote or a registered estimate), and a call with a nonzero estimated cost
escalates to the operator once the rolling 24-hour total would pass the budget.

## The audit ledger

`ledger.py` is one append-only JSONL file in which each line carries the SHA-256 of the line
before it, so any edit or deletion breaks the chain where it happened. Appends are serialized:
the gateway process writes almost every entry, and the orchestrator adds each finished job's
result while holding the same engine lock. Other processes keep their own append-only event
logs: the operator's approvals and verdicts, which the gateway folds into the ledger, and the
job queue and agenda, which are operational state whose tool calls are ledgered. The same file
serves two views: a decision log (actions, asks, verdicts, costs) and a proposal ledger with an
anti-nag state machine, so a declined idea does not come back: "never" stays dead, "not now"
waits out a cooldown, and "park" waits for its stated reopen condition.

## Job orchestration

`jobs.py` is an event-sourced queue (append-only, torn-line tolerant). The orchestrator claims
pending jobs in a single dispatcher thread, so two workers never take the same job, and runs
each one as its own governed agent session in an isolated state folder with its own ledger,
so concurrent jobs never race on the main hash chain.

## Safety rails

- **Kill switch**: one file, `state/STOP`. While it exists the main gateway denies every tool
  call (each denial ledgered), a running job is cancelled between tool rounds, scheduled ticks
  refuse to start and chat answers without opening an engine session. It is a file on purpose:
  it works when chat is wedged, the network is down or an engine is mid-loop, and it survives
  reboots.
- **Spend meter**: the rolling budget above.
- **Inline warden**: for tool classes you opt in, a small second model reviews the call before
  it runs, under a hard timeout so a slow reviewer cannot hang a call. By default it is
  advisory (a veto is ledgered and the call proceeds); in block mode a veto becomes an
  escalation to the operator, never a silent kill.
- **Post-hoc warden and replay**: a separate model reads the ledger after the fact, and
  `scripts/replay_gate.py` re-runs historical decisions against the current policy to show what
  would change.
- **Web and file tools**: `web_fetch` reads public pages only (private, loopback and link-local
  addresses refused, re-checked on every redirect hop); file tools are confined to a workspace.

## Design notes

- **Engines are replaceable; the interface is not.** Provider event shapes stop at the adapter.
- **Memory recall is read-only.** Engines can search a markdown knowledge base but never write
  to it; the only road in is `scripts/consolidate.py`, which turns ledgered observations into
  reviewable drop files.
- **Honest failure over silent fallback.** Autonomous jobs run only on the lane they were
  configured for; a missing key is an error, never a quiet switch to a different model.
- **Stdlib only.** No third-party packages, so the trust boundary is small enough to read.

## Known gaps

Found in a review of this snapshot and left as they were, since the project is parked:

- **Sub-jobs do not run yet.** A job that submits sub-jobs writes them to its own isolated
  queue, which the orchestrator does not drain. (The test of decomposition feeds the main queue
  directly, so it does not catch this.)
- **Chat can approve its own escalation.** So that the operator can approve things by talking,
  the chat engine holds `resolve_approval`, which records an approval in the operator's name.
  That tool is not itself an ask-class call, so an engine steered by content it read could
  approve its own pending escalation. Making `decision_write` an ask class closes it.
- **The Venice chat lane is outside the dollar budget.** Venice bills in its own credits, so
  that lane records tokens but no dollar cost.
- Some comments mention launchers and design notes from the private project that are not in
  this repository.

## Running it

Python 3.11 or later; nothing to install.

```
python run_tests.py                                    # the whole suite
python -m minerva_core.gateway --state-dir state       # the gateway alone (run from src/)
python scripts/supervisor.py                           # the daemons, from one window
```

Credentials live in `config/*.env` files, which are git-ignored; each adapter says which
variables it reads. `MINERVA_HOME` and `MINERVA_BRAIN` point the scripts at the project and at
the knowledge base. The `.bat` launchers are the Windows one-click versions.

## Tests

32 test files and 1,055 checks as of 2026-09-30, standard library only. Each file
runs on its own (`python tests/test_ledger.py`); `run_tests.py` runs them all and fails if any
file fails or finishes without reporting its checks. The GitHub Actions workflow in
`.github/workflows/` runs the same command on Windows and Ubuntu.

## Status

A working prototype, built in June 2026 and parked since then. Its ideas (the
governed gateway, the ledger, the anti-nag index, the kill switch) now live on in a file-based
system run by scheduled agent sessions, so this repository is a snapshot of the runtime, not an
active project. "Round N" in a comment is the build round it came from.

## License

MIT; see `LICENSE`.
