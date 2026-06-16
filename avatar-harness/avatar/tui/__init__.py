"""The interactive Textual cockpit (Phase 3.1 Lane 2, ADR-0002).

Behind the optional `[textual]` extra: the core engine + SDK import without `textual`, so
this package's heavy imports are *not* pulled in by `import avatar`. Use
`load_cockpit()` to obtain the app class with a clear install hint when the extra is absent.
"""


def load_cockpit() -> type:
    """Return the `CockpitApp` class, raising a clear hint if the `textual` extra is missing.

    Returns:
        The `CockpitApp` type.

    Raises:
        RuntimeError: If `textual` is not installed (the optional cockpit extra).
    """
    try:
        from avatar.tui.app import CockpitApp  # noqa: PLC0415 — lazy by design: the guard
    except ModuleNotFoundError as exc:  # textual (or a transitive) is absent
        raise RuntimeError(
            "The interactive cockpit needs the 'textual' extra: pip install 'avatar-harness[textual]'"
        ) from exc
    return CockpitApp
