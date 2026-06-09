"""Generate Mintlify MDX API-reference pages from the package's docstrings.

The code is the single source of truth. This walks ``avatar_harness`` with the
standard library only (no extra dependencies, per the project's minimal-deps
goal), reads each public module/class/function's signature and Google-style
docstring, and emits one MDX page per module under ``docs/api-reference/``. It
also rewrites the "API reference" group in ``docs.json`` so navigation stays in
sync with whatever modules currently exist.

Usage:
    python scripts/gen_api_docs.py            # regenerate pages + nav
    python scripts/gen_api_docs.py --check    # fail if anything is out of date
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pkgutil
import re
import sys
from pathlib import Path
from types import ModuleType

import avatar_harness

DOCS_ROOT = Path("docs")
API_DIR = DOCS_ROOT / "api-reference"
DOCS_JSON = DOCS_ROOT / "docs.json"  # config lives at the docs content root (Mintlify convention)
API_GROUP = "API reference"


def _mdx_safe(text: str) -> str:
    """Escape characters MDX would otherwise parse as JSX (``<`` and braces)."""
    return text.replace("<", "&lt;").replace("{", "&#123;").replace("}", "&#125;")


def _shorten(text: str) -> str:
    """Collapse dotted qualnames to their leaf (``a.b.C`` -> ``C``) for readability."""
    return re.sub(r"\w+(?:\.\w+)+", lambda m: m.group(0).rsplit(".", 1)[-1], text)


def _type_str(annotation: object) -> str:
    """Render a type annotation readably (``str``, ``list[Evidence]``, ``Literal['a']``)."""
    if annotation is None or annotation is inspect.Signature.empty:
        return ""
    if inspect.isclass(annotation):
        return annotation.__name__
    return _shorten(str(annotation).replace("typing.", ""))


def _slug(module_name: str) -> str:
    """Turn ``avatar_harness.tools.search`` into the page slug ``tools-search``."""
    return module_name.removeprefix(avatar_harness.__name__ + ".").replace(".", "-")


def _iter_modules() -> list[ModuleType]:
    """Import and return every non-private submodule of the package, sorted by name."""
    modules: list[ModuleType] = []
    for info in pkgutil.walk_packages(avatar_harness.__path__, avatar_harness.__name__ + "."):
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        modules.append(importlib.import_module(info.name))
    return sorted(modules, key=lambda m: m.__name__)


def _public_members(module: ModuleType, predicate) -> list[tuple[str, object]]:
    """Return ``(name, object)`` pairs defined in ``module`` that match ``predicate``."""
    out = []
    for name, obj in inspect.getmembers(module, predicate):
        if name.startswith("_"):
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue  # skip imported-and-re-exported symbols; document them at their source
        out.append((name, obj))
    return out


def _signature(obj: object) -> str:
    """Best-effort source signature, with dotted qualnames shortened to the leaf name."""
    try:
        sig = str(inspect.signature(obj))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    # Strip volatile object addresses (e.g. a lambda default renders as
    # `<function X.<lambda> at 0x10b8…>`) so the generated output is reproducible —
    # otherwise every run differs and `--check` can never pass.
    sig = re.sub(r" at 0x[0-9a-fA-F]+", "", sig)
    return _shorten(sig)  # `avatar_harness.model_client.ModelClient` -> `ModelClient`


def _docblock(obj: object) -> str:
    """The object's cleaned docstring as MDX-safe Markdown, or an italic placeholder."""
    doc = inspect.getdoc(obj)
    return _mdx_safe(doc) if doc else "_No description._"


def _pydantic_fields(cls: type) -> str:
    """Render a pydantic model's fields as a Markdown table, or '' for non-models."""
    fields = getattr(cls, "model_fields", None)
    if not fields:
        return ""
    rows = ["| Field | Type | Required |", "| --- | --- | --- |"]
    for fname, info in fields.items():
        annotation = _mdx_safe(_type_str(getattr(info, "annotation", None)))
        required = "yes" if info.is_required() else "no"
        rows.append(f"| `{fname}` | `{annotation}` | {required} |")
    return "\n".join(rows)


def _render_class(name: str, cls: type) -> str:
    parts = [f"### `{name}`", "", _docblock(cls)]
    sig = _signature(cls)
    if sig:
        parts += ["", "```python", f"{name}{sig}", "```"]
    table = _pydantic_fields(cls)
    if table:
        parts += ["", "**Fields**", "", table]
    for mname, method in _public_members_of_class(cls):
        msig = _signature(method)
        parts += ["", f"#### `{name}.{mname}{msig}`", "", _docblock(method)]
    return "\n".join(parts)


def _public_members_of_class(cls: type) -> list[tuple[str, object]]:
    """Public methods/properties declared directly on ``cls`` (not inherited)."""
    out = []
    for mname, member in inspect.getmembers(cls):
        if mname.startswith("_") or mname not in vars(cls):
            continue
        if inspect.isfunction(member) or isinstance(member, property):
            out.append((mname, member))
    return out


def _render_function(name: str, fn: object) -> str:
    return f"### `{name}{_signature(fn)}`\n\n{_docblock(fn)}"


def _render_module(module: ModuleType) -> str:
    """Build the full MDX page body for one module."""
    title = module.__name__
    summary = (inspect.getdoc(module) or "").split("\n\n")[0].replace("\n", " ")
    blocks = [f"---\ntitle: {title}\n---", "", _mdx_safe(inspect.getdoc(module) or "")]

    classes = _public_members(module, inspect.isclass)
    functions = _public_members(module, inspect.isfunction)
    if classes:
        blocks += ["", "## Classes", *[_render_class(n, c) for n, c in classes]]
    if functions:
        blocks += ["", "## Functions", *[_render_function(n, f) for n, f in functions]]
    _ = summary  # reserved for future front-matter description
    return "\n".join(blocks) + "\n"


def _write_or_check(path: Path, content: str, *, check: bool) -> bool:
    """Write ``content`` to ``path``; in check mode, only report whether it differs."""
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return False
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return True


def _sync_nav(slugs: list[str], *, check: bool) -> bool:
    """Point the docs.json "API reference" group at exactly the generated pages."""
    config = json.loads(DOCS_JSON.read_text(encoding="utf-8"))
    pages = [f"api-reference/{s}" for s in slugs]
    changed = False
    for tab in config.get("navigation", {}).get("tabs", []):
        for group in tab.get("groups", []):
            if group.get("group") == API_GROUP and group.get("pages") != pages:
                group["pages"] = pages
                changed = True
    if changed and not check:
        DOCS_JSON.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    """Regenerate the API pages (or, with --check, verify they are up to date)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if pages are stale.")
    args = parser.parse_args(argv)

    modules = [
        m
        for m in _iter_modules()
        if _public_members(m, inspect.isclass) or _public_members(m, inspect.isfunction)
    ]
    slugs = [_slug(m.__name__) for m in modules]

    stale = False
    for module, slug in zip(modules, slugs, strict=True):
        if _write_or_check(API_DIR / f"{slug}.mdx", _render_module(module), check=args.check):
            stale = True
    if _sync_nav(slugs, check=args.check):
        stale = True

    if args.check and stale:
        print("API docs are out of date — run `make docs-api` and commit the result.")
        return 1
    print(f"{'checked' if args.check else 'generated'} {len(slugs)} API page(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
