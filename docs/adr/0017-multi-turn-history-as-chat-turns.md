# ADR 0017 — Multi-turn conversation history as real chat turns

- **Status:** Accepted — implemented 2026-06-15
- **Date:** 2026-06-15
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0002 (interactive cockpit, multi-turn REPL session); `HARNESS_DESIGN.md` §9 (the per-turn context packet), §23 (the session scope above one task); invariant #1 (`TaskState` is the source of truth; the model's message history is *derived* from it). Surfaced by a dogfood cockpit session.

## Context

A REPL session (§23) runs each goal as a fresh `TaskState` through the single-task engine; `ReplSession._seed_history` carried the prior conversation forward by appending each past turn onto the new task as `Evidence(kind="history")`. The context builder (§9) then flattened that evidence into "Recent evidence" bullets inside the **single user message** the model receives — `model_client.build_messages` emitted exactly `[system, user(packet)]`, with no real conversational turns.

The model under-weights history presented this way. In a dogfood sitting it asked the same clarifying question on two consecutive goals: the first goal ended on an `ask_user`, the user answered in the next message, but the answer arrived as a fresh contextless goal while the prior exchange sat buried among evidence bullets — so the model re-asked. Conversation history that looks like incidental "evidence" reads as lower-priority context than the current instruction; a chat model is trained to weight prior `user`/`assistant` turns as the live thread.

## Decision

**Cross-goal conversation history is sent as real `role="user"` / `role="assistant"` messages**, positioned between the system message and the current working packet:

```
[ {system}, {user: prev goal}, {assistant: prev reply}, {user: prev goal2}, …, {user: <current working packet>} ]
```

Concretely:

- A small `ConversationTurn(role, content)` model lives on `TaskState.conversation` (in `state.py`, the low-level module everyone imports, to avoid an import cycle).
- `ReplSession._seed_history` stops adding `kind="history"` evidence and instead populates `task.conversation` from the session `history`, mapping a stored `agent` turn → `"assistant"` and `user` → `"user"`. `@path` grounding is untouched — that is *context*, not conversation.
- `ContextPacket` carries `conversation`; `ContextBuilder.build` copies `state.conversation` onto it.
- `build_messages` inserts one message per conversation turn, in order, between the system message and the final user packet. This applies to **both** transports (native tool-calling and the legacy JSON escape hatch — `build_messages` is shared).

The **current** goal stays inside the §9 derived working packet (the final user message, with phase / evidence / tools); only *prior* turns precede it as standalone messages.

This **refines, not contradicts, invariant #1.** `TaskState` is still the source of truth: `conversation` is a field on it, and the chat messages are *derived* from that field each turn, exactly as the working packet is. We are changing the *shape* of the derivation (real turns vs. flattened bullets), not introducing a second source of truth or letting the transcript drive state.

## Consequences / alternatives

- The model now sees the prior exchange as a first-class conversational thread, so a follow-up that answers a previous `ask_user` is read as a continuation rather than a fresh, contextless goal — the dogfood double-ask is closed.
- The within-task working set is unchanged: evidence, action history, and the verifier pin still flow through the §9 packet as before. Only cross-goal turns moved out of it.
- The `history`-evidence path is gone; the session-state regression tests now assert on `state.conversation` (the blocked goal's *question* appears as an assistant turn, not the string `"blocked"`).
- *Rejected — keep history as flattened "Recent evidence" bullets.* This is the status quo that caused the re-prompting: the model under-weights conversation presented as evidence. Cheap to keep, but it does not fix the observed failure.
- *Rejected — make the transcript the source of truth.* That would invert invariant #1 (the defining inversion of this harness). Deriving the messages from `TaskState.conversation` keeps state explicit, structured, and replayable.
