"""jo — the interactive Textual cockpit, a reference coding agent over `avatar` (ADR-0002, ADR-0023).

The cockpit is one consumer of the harness core: it depends on the `avatar-harness`
distribution and consumes only its public surface (`Harness` / `ReplSession` / `Session`
+ the typed `HarnessEvent`s). The import direction is strictly consumer → core — nothing
in `avatar` imports `jo`.

`load_cockpit()` returns the app class lazily so that `replay.py` (which carries no Textual
import) stays usable wherever events are, without forcing a Textual import at package load.
"""


def load_cockpit() -> type:
    """Return the `CockpitApp` class, raising a clear hint if `textual` is somehow absent.

    Returns:
        The `CockpitApp` type.

    Raises:
        RuntimeError: If `textual` is not importable (it is a hard dependency of `jo-cli`,
            so this normally indicates a broken install).
    """
    try:
        from jo.app import CockpitApp  # noqa: PLC0415 — lazy by design: keeps Textual off package load
    except ModuleNotFoundError as exc:  # textual (or a transitive) is absent
        raise RuntimeError("The cockpit needs 'textual'; reinstall the cockpit: pip install jo-cli") from exc
    return CockpitApp
