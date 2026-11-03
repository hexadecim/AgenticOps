"""File-based adapters: runbooks as Markdown with YAML front matter, logs as `<service>.log` files.

Log lines are masked before they leave this module (domain.masking); runbooks name allow-listed actions per
step in their front matter, which is the only way an action becomes proposable.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import structlog
import yaml

from ...domain.masking import mask
from ...domain.model import LogFinding, Runbook, RunbookStep

log = structlog.get_logger(__name__)
_PROBLEM = re.compile(r"\b(ERROR|WARN|WARNING|FATAL|CRITICAL)\b")
_TIME = re.compile(r"^(\d{2}:\d{2}(?::\d{2})?)")
_SHAPE = re.compile(r"\d+")


def parse_runbook(text: str, source: str = "") -> Runbook | None:
    if not text.startswith("---"):
        return None
    _, front, body = [*text.split("---", 2), "", ""][:3]
    meta = yaml.safe_load(front) or {}
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    steps = tuple(
        RunbookStep(int(s.get("step", 0)), str(s["action"]), str(s.get("when", "")).strip())
        for s in meta.get("actions", []) or []
        if isinstance(s, dict) and s.get("action")
    )
    sections: list[tuple[str, str]] = []
    heading, lines = "Overview", list[str]()
    for line in body.splitlines():
        if line.startswith("#"):
            if "".join(lines).strip():
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = line.lstrip("#").strip(), []
        else:
            lines.append(line)
    if "".join(lines).strip():
        sections.append((heading, "\n".join(lines).strip()))
    return Runbook(
        id=str(meta["id"]),
        title=str(meta.get("title", meta["id"])),
        services=tuple(str(s) for s in meta.get("services", []) or []),
        steps=steps,
        sections=tuple(sections),
        source=source,
    )


class MarkdownRunbooks:
    async def load(self, folder: str) -> list[Runbook]:
        return await asyncio.to_thread(self._load, folder)

    @staticmethod
    def _load(folder: str) -> list[Runbook]:
        path = Path(folder)
        if not path.is_dir():
            log.warning("runbook_folder_missing", folder=folder)
            return []
        out = []
        for file in sorted(path.glob("*.md")):
            try:
                rb = parse_runbook(file.read_text(encoding="utf-8"), file.name)
            except (yaml.YAMLError, ValueError, KeyError) as exc:
                log.warning("runbook_unreadable", file=file.name, error=str(exc))
                continue
            if rb:
                out.append(rb)
        return out


def findings(service: str, lines: list[str], *, limit: int = 3) -> LogFinding | None:
    """The last `limit` problem lines of distinct shape (numbers ignored), how many more there were, and the
    time of the first one. Every line is masked."""
    problems = [mask(line) for line in lines if _PROBLEM.search(line)]
    if not problems:
        return None
    shown: list[str] = []
    shapes: set[str] = set()
    for line in reversed(problems):
        shape = _SHAPE.sub("#", line[9:] if _TIME.match(line) else line)
        if shape not in shapes:
            shapes.add(shape)
            shown.insert(0, line)
        if len(shown) == limit:
            break
    first = _TIME.match(problems[0])
    return LogFinding(service, tuple(shown), len(problems) - len(shown), first.group(1) if first else None)


class FileLogs:
    """`<folder>/<service>.log`: the last 2000 lines of each are searched."""

    async def search(self, folder: str, services: list[str], *, limit: int = 3) -> list[LogFinding]:
        return await asyncio.to_thread(self._search, folder, services, limit)

    @staticmethod
    def _search(folder: str, services: list[str], limit: int) -> list[LogFinding]:
        out = []
        for service in services:
            file = Path(folder) / f"{Path(service).name}.log"  # never leave the folder
            if not file.is_file():
                continue
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
            found = findings(service, lines, limit=limit)
            if found:
                out.append(found)
        return out
