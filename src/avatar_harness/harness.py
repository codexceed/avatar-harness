"""Harness — the importable facade over the agent loop (§20, Phase 2.6).

`Harness` performs the default collaborator wiring the CLI used to hardcode in
`run_agent`: the tool registry, model client, context builder, verifier, and
permission policy. Every seam is overridable via a constructor kwarg, so a
downstream user can swap a provider, a tool set, the verification contract, or
the permission gate without touching the core. `from_env` builds a
`HarnessConfig` from the environment (the same `AVATAR_*` settings the CLI uses)
and applies the same defaults.

The facade only *assembles* the run; the `AgentRunner` still owns all state
mutation (invariant #2). `run` returns the terminal `TaskState`, identical to
what `cli.run_agent` returns, so the CLI can delegate straight through.
"""

from typing import Literal

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.events import Emitter
from avatar_harness.model_client import ModelClient, OpenAIModelClient
from avatar_harness.permission import PermissionPolicy
from avatar_harness.runner import AgentRunner
from avatar_harness.session import Session
from avatar_harness.state import TaskState
from avatar_harness.tools import default_registry
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


class Harness:
    """The importable facade: default wiring with every collaborator overridable.

    Each collaborator defaults to the standard MVP wiring (the same the CLI used to
    construct inline) and is replaceable via its kwarg — the Principle-A seam. The
    facade constructs nothing on the run's behalf that the runner should own; it only
    assembles the collaborators.

    Args:
        config: Harness config (budgets, workspace root, commands, denylist).
        model: Model client; a default `OpenAIModelClient(config)` if omitted.
        tools: The active `ToolRegistry`; `default_registry()` if omitted.
        verifier: The completion verifier; `Verifier(config)` if omitted.
        policy: The before-tool-call permission gate; the standard tier policy
            (threaded with `config.sensitive_path_globs`) if omitted.
        context_builder: The per-turn context assembler; `ContextBuilder()` if omitted.
        emitter: The observation-only event emitter; a fresh `Emitter()` if omitted.
    """

    def __init__(  # noqa: PLR0913 — keyword-only dependency injection of the run's collaborators
        self,
        *,
        config: HarnessConfig,
        model: ModelClient | None = None,
        tools: ToolRegistry | None = None,
        verifier: Verifier | None = None,
        policy: PermissionPolicy | None = None,
        context_builder: ContextBuilder | None = None,
        emitter: Emitter | None = None,
    ) -> None:
        self.config = config
        self.model = model or OpenAIModelClient(config)
        self.tools = tools or default_registry()
        self.verifier = verifier or Verifier(config)
        self.policy = policy
        self.context_builder = context_builder or ContextBuilder()
        self.emitter = emitter or Emitter()

    @classmethod
    def from_env(
        cls,
        *,
        model: ModelClient | None = None,
        tools: ToolRegistry | None = None,
        verifier: Verifier | None = None,
        policy: PermissionPolicy | None = None,
        context_builder: ContextBuilder | None = None,
        emitter: Emitter | None = None,
    ) -> "Harness":
        """Build a `Harness` with config read from the environment + defaults.

        `HarnessConfig()` reads the `AVATAR_*` settings; every collaborator keeps its
        kwarg override so a fully default-but-fake-model harness is one call.

        Args:
            model: Model client override; a default `OpenAIModelClient` if omitted.
            tools: Tool registry override; `default_registry()` if omitted.
            verifier: Verifier override; `Verifier(config)` if omitted.
            policy: Permission policy override; the standard tier policy if omitted.
            context_builder: Context builder override; `ContextBuilder()` if omitted.
            emitter: Event emitter override; a fresh `Emitter()` if omitted.

        Returns:
            A `Harness` wired from the environment config.
        """
        return cls(
            config=HarnessConfig(),
            model=model,
            tools=tools,
            verifier=verifier,
            policy=policy,
            context_builder=context_builder,
            emitter=emitter,
        )

    def _build_runner(self, allow_dirty: bool) -> AgentRunner:
        """Assemble a run-scoped `AgentRunner` (shared by `run`/`arun`/`session`).

        Constructs the `Workspace`/`RunDeps` the same way `cli.run_agent` did — threading
        `config.sensitive_path_globs` into the workspace as the secure-by-default backstop.

        Args:
            allow_dirty: When `True`, open the workspace despite uncommitted tracked changes (§15).

        Returns:
            A fresh `AgentRunner` wired with this facade's collaborators.
        """
        deps = RunDeps(
            workspace=Workspace(
                self.config.workspace_root,
                allow_dirty=allow_dirty,
                sensitive_path_globs=self.config.sensitive_path_globs,
            ),
            config=self.config,
            cancellation=CancellationToken(),
        )
        return AgentRunner(
            model_client=self.model,
            registry=self.tools,
            deps=deps,
            context_builder=self.context_builder,
            verifier=self.verifier,
            emitter=self.emitter,
            config=self.config,
            policy=self.policy,
        )

    def run(
        self,
        task: str,
        *,
        task_kind: Literal["edit", "investigate", "test_only"] = "investigate",
        allow_dirty: bool = False,
    ) -> TaskState:
        """Run the agent loop over `task` synchronously and return the terminal `TaskState`.

        The batch/library entry point; for an interactive UI use `session` (observation +
        control) or `arun` (the bare async loop).

        Args:
            task: The natural-language task to run.
            task_kind: The verification contract to apply (`investigate` / `edit` / `test_only`).
            allow_dirty: When `True`, open the workspace despite uncommitted tracked changes (§15).

        Returns:
            The terminal `TaskState` after the loop settles.
        """
        return self._build_runner(allow_dirty).run(TaskState(goal=task, task_kind=task_kind))

    async def arun(
        self,
        task: str,
        *,
        task_kind: Literal["edit", "investigate", "test_only"] = "investigate",
        allow_dirty: bool = False,
    ) -> TaskState:
        """Async twin of `run` — the bare loop, for callers already on an event loop.

        Use `session` instead when you also want the typed event stream + approval/cancel
        controls; `arun` returns only the terminal state.

        Args:
            task: The natural-language task to run.
            task_kind: The verification contract to apply (`investigate` / `edit` / `test_only`).
            allow_dirty: When `True`, open the workspace despite uncommitted tracked changes (§15).

        Returns:
            The terminal `TaskState` after the loop settles.
        """
        return await self._build_runner(allow_dirty).arun(TaskState(goal=task, task_kind=task_kind))

    def session(
        self,
        task: str,
        *,
        task_kind: Literal["edit", "investigate", "test_only"] = "investigate",
        allow_dirty: bool = False,
    ) -> Session:
        """Open an interactive `Session` over `task` — the two-plane SDK surface (§13, §23).

        Returns a not-yet-started session: drive it with `await session.run()` while
        consuming `session.events()` (observation) and calling `session.resolve_approval()`
        / `session.cancel()` (control). This is the shape a TUI or autonomous wrapper binds to.

        Args:
            task: The natural-language task to run.
            task_kind: The verification contract to apply (`investigate` / `edit` / `test_only`).
            allow_dirty: When `True`, open the workspace despite uncommitted tracked changes (§15).

        Returns:
            A `Session` wrapping the run — observation out, control in.
        """
        runner = self._build_runner(allow_dirty)
        return Session(runner, TaskState(goal=task, task_kind=task_kind))
