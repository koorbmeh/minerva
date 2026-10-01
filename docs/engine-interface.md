# Minerva: engine interface and ledger

The design spec for Minerva's permanent layer: the seam between Minerva and whatever model runs
the agent loop, the events that cross it, the governance gate every tool call passes, and the
ledger that records it all. Written in June 2026 alongside the first code and corrected here
against the code as it stood when the project was parked. Code: `src/minerva_core/`. Companion:
[the architecture](architecture.md), sections 2b to 2d.

The code cites this document by section number ("spec §3"), so the numbers below are the ones it
means.

## 0. Purpose

This is the seam. Everything above it (governance, memory, drives, persona, the ledger) is
Minerva and is meant to last; everything below it (the agent loop) is an **engine** and is
replaceable. An engine is anything that can hold a session, take a prompt, stream events and call
tools: a hosted agent runtime today, a frontier model's native agent backend tomorrow. Swap the
engine; don't rewrite Minerva.

**Principles**

1. **Engines are untrusted.** Governance never lives inside an engine, however good the engine's
   own approval settings look. The trust boundary is Minerva's.
2. **Tools reach engines only through Minerva's gateway.** The gateway speaks MCP. Each engine is
   configured with the gateway as its only tool source, its own built-in tools switched off, and
   an allowlist of the gateway tools it may see. Every tool call passes `GovernanceGate.check()`
   and is ledgered.
3. **Memory is canonical on Minerva's side.** Engine-native long-term memory is switched off.
   Engines read the knowledge base through a read-only search tool (`brain_recall`) and write
   nothing to it; observations they record (`episodic_note`) reach it only through a
   reviewable consolidation step (`scripts/consolidate.py`).
4. **Events are the contract.** Adapters translate each engine's native stream into the small
   event taxonomy in section 2; nothing upstream knows which engine produced an event.
5. **Everything auditable lands in one ledger**: append-only JSONL, hash-chained. The decision log
   and the proposal (idea) ledger share it.
6. **Standard library only.** `minerva_core` has no third-party dependencies and runs on Windows
   and Linux (CI runs the suite on both). Adapters were allowed dependencies; in practice they
   are stdlib-only too.

## 1. The interface

Defined in `src/minerva_core/engine.py` as typed Protocols (structural: an adapter needs the
methods, not a base class):

```python
class EngineAdapter(Protocol):
    name: str
    def capabilities(self) -> set[str]: ...      # {"stream", "tools_mcp", "model_per_session", ...}
    def create_session(self, cfg: SessionConfig) -> EngineSession: ...

class EngineSession(Protocol):
    id: str
    def send(self, text: str) -> Iterator[EngineEvent]: ...   # yields until DONE; always terminates
    def cancel(self) -> None: ...
    def close(self) -> None: ...
```

`SessionConfig` carries the persona text, a model hint, `RoutingHints` and the session's tool
allowlist. `RoutingHints` holds the routing dimensions from the architecture's section 2d:

| Field | Values | Used for |
| --- | --- | --- |
| `sensitivity` | `normal`, `private` | privacy routing (below) |
| `capability` | `light`, `standard`, `heavy`, `max` | the model tier on lanes that pick a model per task |
| `guardrail_tier` | `frontier`, `hosted-open`, `local` | which kind of provider may see the work |
| `modality` | `text`, `image`, `video` | generation needs an engine that advertises it |
| `cost_cap_usd` | a dollar figure | declared for the broker; nothing enforces it yet (section 6) |

`EngineRegistry.pick()` is the router, deliberately simple so the ledger can show what smarter
routing should look like:

- `guardrail_tier` `local` is honored strictly: no local engine registered means an error, never
  a quiet fall-through to a frontier model.
- `hosted-open` (aliases `open` and `uncensored`) is honored just as strictly, for work where a
  frontier provider's guardrails would get in the way.
- `sensitivity` `private` forces a local engine only when the operator has set
  `BrokerPolicy.private_off_frontier`; by default private work may use the frontier lane.
- `modality` `image` or `video` needs an engine advertising `image_gen` or `video_gen`.
- Everything else goes to the default engine.

`EngineRegistry.route()` is the one call site for "which engine gets which job": it calls `pick()`
and, when the adapter offers `model_for_hints()`, fills in the model for the requested tier.

## 2. Event taxonomy

Defined in `src/minerva_core/events.py`. The right-hand column is how the Hermes adapter maps the
Agent Client Protocol stream, as observed live in June 2026; the other adapters build the same
events from their providers' responses.

| `EventType` | Meaning | Payload | Hermes over ACP |
| --- | --- | --- | --- |
| `text_delta` | a streamed chunk of the reply | `text` | `agent_message_chunk` |
| `thought` | a reasoning chunk, never shown as the reply | `text` | `agent_thought_chunk` |
| `message` | the completed reply | `text` | assembled when the prompt's response arrives |
| `tool_call` | the engine invoked a tool | `tool`, `args`, `call_id` | `tool_call` |
| `tool_result` | the result handed back | `call_id`, `ok`, `result` | `tool_call_update` once completed or failed |
| `ask_user` | something needs the operator | `reason`, `question`, `ref` | `session/request_permission`, which the adapter denies at once and reports (never seen live) |
| `cost` | a metering tick | `input_tokens`, `output_tokens`, `usd` | the response's token usage; `usage_update` reports context fill instead |
| `error` | an engine fault | `message`, `fatal` | transport errors, error responses, timeouts |
| `done` | the turn finished | (a stop reason) | the end of the prompt |

Adapters must emit `cost` whenever the engine reports usage, and must surface tool activity even
when the engine runs a tool internally; a tool an engine would hide from its stream is simply not
allowlisted to it. Native event kinds an adapter does not know are skipped, never guessed at, and
the Hermes adapter collects them (`unknown_kinds`) for its probe report. Translation lives in one
function per Hermes adapter (`_translate`), so a change in the engine's protocol costs one edit.

## 3. Governance interception

`GovernanceGate.check(tool, args, classes, est_cost_usd, session_id)` runs in the gateway
(`src/minerva_core/governance.py`) **before** any tool handler. Tools declare their own action
classes and estimated cost when they register (`ToolSpec.classes`, `ToolSpec.est_cost_usd`);
the gateway, not the engine, owns that metadata, so a misclassified tool is the security bug. The
gate decides in this order:

1. **Kill switch.** If `state/STOP` exists, every call is denied, and each denial is ledgered.
2. **Deny list.** A tool on the gate's deny list is refused. The gate supports one; the shipped
   gateway sets none, and `scripts/replay_gate.py` shows what one would have changed.
3. **Escalate** (ask the operator) when the tool's classes include any of `ASK_CLASSES`
   (irreversible, spend, outbound communication, anything affecting another person, physical,
   medically or legally consequential), when it is estimated to cost more than $5 (the
   gateway's `--spend-threshold`, or `MINERVA_SPEND_THRESHOLD`), or when it would take the
   spend over the rolling 24-hour window past the daily budget of $5
   (`MINERVA_DAILY_BUDGET_USD`; see `SpendMeter` in `src/minerva_core/spend.py`).
4. **Inline warden**, only for classes the operator opts in (`MINERVA_INLINE_CLASSES`; none by
   default): a small second model reviews the would-be-allowed call, with a hard
   timeout of 3 seconds (`MINERVA_INLINE_TIMEOUT_S`). In the default advisory mode a veto
   is ledgered and the call proceeds; in block mode (`MINERVA_INLINE_MODE`) a veto becomes an
   escalation, and an unreachable reviewer escalates too unless `MINERVA_INLINE_FAIL` says
   allow. Calls the operator directed (origin `operator`, or an adopted pitch) are never
   second-guessed.
5. **Allow**, and ledger it.

There is no environment switch for the ask classes: widening them means editing
`governance.ASK_CLASSES`, which escalates that class for every tool and every engine.

**The approvals rail.** An escalated call is fingerprinted (SHA-256 of its canonical tool and
arguments), queued for the operator in `state/approvals.jsonl`, and the engine is told to stand
down. While the ask is pending, an identical call does not queue again. Once the operator
approves, the next identical call runs exactly once: the gate writes a "consumed" line and a
`verdict` entry parented to the original ask. A denial stands for that fingerprint; different
arguments are a different fingerprint and a fresh ask. Every check writes a ledger entry: `action`
for allow and deny, `ask` for an escalation, `verdict` for a consumed approval.

The operator's answers arrive through append-only files (`state/approvals.jsonl`,
`state/verdicts.jsonl`). The host loop writes them from the console and from Discord replies; in
chat, two gateway tools, `record_verdict` and `resolve_approval`, write them in the operator's
name. The gateway folds them into the ledger, so the hash chain keeps a single writer. Because the
chat engine holds `resolve_approval`, it could approve its own pending escalation; that is one of
the [known gaps](../README.md#known-gaps) in the README. Each answer is also a labelled example of
the operator's judgment, which was the intended training data for a model of the operator (never
built; see the architecture's section 0).

## 4. The ledger (one spine, two views)

`src/minerva_core/ledger.py`: append-only JSONL at `state/ledger.jsonl`. Each line is
`{id, ts, type, actor, parent_id, payload, prev_hash}`, serialized with sorted keys, where
`prev_hash` is the SHA-256 of the previous raw line (the first line's is 64 zeros). Editing,
inserting or deleting a line breaks the chain at the line after it; `Ledger.verify_chain()` walks
the file and returns whether it holds, how many entries it read and the first bad line. Nothing
anchors the end of the chain, though: cutting entries off the end, or rewriting every line after
an edit, goes undetected unless the latest hash is also kept somewhere else, which was never built
(section 6).

**Entry types:** `session`, `action`, `tool_call`, `ask`, `verdict`, `pitch`, `pitch_verdict`,
`outcome`, `cost`, `note`.

The **decision-log view** is the action, ask, verdict and cost entries. The **proposal-ledger
view** is the pitch, pitch-verdict and outcome entries, with state derived by `ProposalIndex`:

```
pitch ──verdict──► adopted
      │          ► dead       (decline_forever)
      │          ► cooling    (decline_for_now) ──cooldown ends──► pitchable again
      │          ► parked ──a note marked "reopen"──► pitchable again
      └─outcome──► tried
```

**Anti-nag.** `ProposalIndex.can_pitch(domain, title_or_slug)` is the gate an idea passes before
the operator ever sees it, and the engine can call it through the `pitch_idea` tool, so a refused
idea comes back with its reason. An idea is refused when a similar one (the same slug, or a
token-Jaccard similarity of at least 0.6 over the slug's words of three letters or more) is already
open, adopted, dead, parked, or still cooling. There can be at most 2 open pitches per domain, with
at least 24 hours between pitches in one domain. A `decline_for_now` cooldown defaults to 30 days.
"Never" means never; a parked idea comes back only through an explicit reopen note.

**One writer per chain.** Within a state folder, the gateway process writes the ledger; the job
orchestrator adds each finished job's note while holding the same engine lock the other
main-folder processes take. Each job runs in its own state folder with its own ledger, so
concurrent jobs never race on one chain. The rule is kept by how processes are started and by that
lock, not by a lock on the file itself: two gateways started on one state folder would fork the
chain.

## 5. Adapters

| # | Adapter | How it talks | State at the park |
| --- | --- | --- | --- |
| 1 | Hermes Agent, `src/minerva_core/adapters/hermes_acp.py` | `hermes acp`: JSON-RPC 2.0 over stdio, streaming | live-tested against hermes-agent 0.16.0; the free lane |
| 2 | Anthropic Messages API with the operator's own key, `src/minerva_core/adapters/anthropic_byok.py` | HTTPS; the adapter runs the agent loop and executes every tool call against a gateway it spawns | the frontier lane, with a model per tier |
| 3 | Venice, `src/minerva_core/adapters/venice.py` | OpenAI-compatible chat completions; the same loop-in-adapter design | the hosted-open lane, and image and video generation |
| – | A minimal loop over any OpenAI-compatible endpoint | – | never built: lanes 2 and 3 already run their own loops |

The first Hermes adapter, `src/minerva_core/adapters/hermes.py`, was written against the HTTP run
API the documentation described; on a live install that API did not exist, so it is kept only as
the mock-tested reference for the translation layer and the ACP adapter replaced it. On the Hermes
side, `scripts/configure_minerva.ps1` applies the reference config,
`config/hermes-config-minerva.yaml` (engine memory, the user profile and the skill curator off,
approvals manual), and disables Hermes's terminal, web, browser and memory toolsets. The gateway
is registered by hand with `hermes mcp add` as Hermes's only MCP server; that registration's
`MINERVA_TOOLS` is the lane's allowlist, and `scripts/enable_brain_web.ps1` widens it. The config
file itself holds no MCP registration.

Engine state is disposable by design. Minerva's state is the ledger, its event logs, the knowledge
base and its configuration, so killing an engine session and rebuilding context from Minerva's side
always works; that is also what makes the whole thing portable between hosts.

## 6. Open at the park

1. Similarity is lexical (slug words); the plan was embeddings behind the same schema.
2. The anti-nag defaults (two open pitches, a day between pitches, a 30-day cooldown) are starting
   values that were never tuned against real use.
3. `RoutingHints.cost_cap_usd` is carried but not enforced; the budget that is enforced is the
   rolling daily one in section 3.
4. The inline warden shipped with no class opted in, so it was never exercised live.
5. The boundary between external content and instructions is advisory (it rides in tool
   descriptions and prompts), not structural.
6. A few ACP messages were never observed live (`plan` and mode updates, a failed tool update, a
   permission request); the Hermes adapter marks them with `CONTRACT-TODO`.
7. Nothing anchors the end of the ledger's hash chain (a latest hash kept outside the file), so
   truncation or a rewrite of the tail goes undetected.
8. The gaps found in a later review of this snapshot are listed in the
   [README](../README.md#known-gaps).

## History

- 2026-06-09: v0.1, with the first code: the interface, the event taxonomy, the gate, the
  hash-chained ledger and the proposal state machine; the gateway and the first Hermes adapter
  the same day.
- June 2026: the ACP adapter replaced the HTTP one; the frontier and hosted-open adapters, the
  `thought` event, approval-honoring retry, the spend meter, the kill switch and both wardens
  followed. The project was parked on 2026-06-17.
- 2026-09-30: this public edition, rewritten from the private original and checked against the
  code.
