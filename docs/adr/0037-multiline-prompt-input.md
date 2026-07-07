# ADR 0037 — Multi-line prompt input: a `TextArea` with edge-gated history and a universal newline key

- **Status:** Accepted — implemented 2026-07-07 (PR #105)
- **Date:** 2026-07-07
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8)
- **Related:** ADR-0002 (the Textual cockpit); `jo-cli/jo/app.py` (`HistoryInput`);
  `jo-cli/ARCHITECTURE.md`; `tests/test_cockpit.py`

## Context

The cockpit's prompt box was a single-line Textual `Input` with `↑`/`↓` recall of the sitting's
submitted prompts. Real goals are often multi-line — a paragraph of intent, a pasted stack trace,
a bulleted spec. A single-line box forces them onto one wrapped line with no way to insert a
deliberate newline, and the moment the user reaches for the obvious "newline without submitting"
gesture there is a terminal-compatibility trap waiting.

Three decisions collide once we go multi-line:

1. **Which widget.** `Input` is single-line by construction; multi-line composition means either
   bolting line handling onto it or moving to `TextArea`.
2. **How you insert a newline vs. submit.** "Enter submits" is the cockpit's established gesture
   (one keystroke launches a goal). A newline therefore needs a *different* key — but the natural
   candidate, `Shift+Enter`, is byte-indistinguishable from `Enter` on most terminals: it only
   arrives as a distinct key under the enhanced (kitty) keyboard protocol, which many terminals
   (VS Code's integrated terminal, Terminal.app, older iTerm2) do not emit.
3. **What `↑`/`↓` do now.** In a single-line box, `↑`/`↓` unambiguously mean "history." In a
   multi-line draft they *also* mean "move the cursor between lines." The two meanings fight.

## Decision

**Subclass `TextArea` as `HistoryInput`**, and take three positions:

1. **`TextArea` over `Input`.** Multi-line editing, soft-wrap, and auto-grow (to a max height,
   then scroll) come for free; we override key handling rather than re-implement an editor.
   `TextArea` has no `Submitted` message, so `HistoryInput` posts its own (carrying `.value` so
   the cockpit's submit handler reads it exactly like the old `Input.Submitted.value`).

2. **`Ctrl+J` is the universal newline; `Shift+Enter` / `Alt+Enter` are conveniences layered on
   top.** `Ctrl+J` is literally LF and is distinct from `Enter` on *every* terminal, so it is the
   guaranteed path. `Shift+Enter` and `Alt+Enter` are routed through the same handler branch for
   users whose terminal *does* speak the enhanced protocol (or who bind the key to the kitty CSI-u
   sequence), but nothing depends on them. `Enter` still submits the whole buffer.

3. **History recall is edge-gated, not modifier-gated.** `↑` recalls an older prompt *only* when
   the cursor is on the first line; `↓` a newer one *only* on the last line. Anywhere in between,
   the arrows move the cursor. Stepping past the newest entry restores the in-progress draft that
   was stashed when browsing began. History stays in-memory for the sitting (the journal is the
   durable record).

## Alternatives considered

- **Keep `Input`, add a separate "expand" affordance / modal editor.** Rejected: a mode switch
  for something as routine as a two-line prompt is friction; inline growth is what users expect.
- **Make `Shift+Enter` the *primary* newline key.** Rejected: it silently fails on the most common
  terminals (it is received as plain `Enter`, so the buffer submits instead of growing) — exactly
  the dogfood symptom that motivated pinning the universal path to `Ctrl+J`. `Shift+Enter` remains
  as a convenience, never the contract.
- **Gate history behind a modifier (e.g. `Alt+↑`) and leave bare `↑`/`↓` as pure cursor movement.**
  Rejected: bare-arrow recall is a muscle-memory expectation from shells and the prior single-line
  box; edge-gating preserves it while still yielding to cursor movement inside a draft — no new
  chord to learn.

## Consequences

- Multi-line goals compose naturally; single-line drafts behave exactly as before (`Enter`
  submits, bare `↑`/`↓` recall — a single-line buffer's only line is both first and last).
- The newline gesture is terminal-portable by default. Users on kitty-protocol terminals get
  `Shift+Enter` too; users elsewhere are told (README, placeholder) to use `Ctrl+J`.
- The Textual floor rises to `>=8` for the `TextArea` APIs used (`cursor_at_first_line` /
  `cursor_at_last_line`, `document.end`).
- Tests assert the handler branches directly via `Pilot.press` (which injects key events by name,
  bypassing terminal encoding), so `Shift+Enter` / `Alt+Enter` are exercised even in CI on a
  terminal that would never emit them.
