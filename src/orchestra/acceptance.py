"""Acceptance criteria: the part of "done" both sides can read.

Two classes only, because they need different machinery:

* ``check`` - a command decides. The agent can run it before committing, the
  gate can run it, the reviewer can re-run it. Nobody has to trust prose.
* ``judge`` - a human or a critic decides from evidence. The system can only
  check that evidence was *declared*, never that it is true.

Anything that fits neither is not a criterion, it is a wish - and saying so out
loud is the point of this module. A task with no criteria is a task whose quality
is undefined, which is exactly the failure this replaces.

Wire format, one line per criterion::

    A1|check|python -m unittest tests.test_gate -k scope|scope gate rejects escapes
    A2|judge|the error message names the file and line|reviewer reads the diff

The id may be omitted (assigned A1, A2, ... in order), the note may be omitted,
and a bare string is read as a ``check`` command.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

CLASSES = ("check", "judge")
ID_PATTERN = re.compile(r"^A\d+$")


class AcceptanceError(ValueError):
    """A criterion line that cannot be trusted to mean anything."""


def parse(items: Iterable[str] | None) -> list[dict]:
    parsed: list[dict] = []
    for raw in items or []:
        text = str(raw).strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split("|")]
        if len(parts) == 1:
            entry = {"id": f"A{len(parsed) + 1}", "class": "check", "criterion": parts[0], "note": ""}
        elif len(parts) < 3:
            raise AcceptanceError(
                f"a criterion is 'id|class|criterion[|note]' or a bare command, got {text!r}"
            )
        else:
            entry = {
                "id": parts[0],
                "class": parts[1].lower(),
                "criterion": parts[2],
                "note": "|".join(parts[3:]).strip(),
            }
        if not ID_PATTERN.match(entry["id"]):
            raise AcceptanceError(f"criterion id must look like A1, got {entry['id']!r}")
        if entry["class"] not in CLASSES:
            raise AcceptanceError(
                f"criterion {entry['id']}: class {entry['class']!r} is not one of {', '.join(CLASSES)}"
            )
        if not entry["criterion"]:
            raise AcceptanceError(f"criterion {entry['id']} has an empty criterion")
        parsed.append(entry)

    seen: set[str] = set()
    for entry in parsed:
        if entry["id"] in seen:
            raise AcceptanceError(f"duplicate criterion id: {entry['id']}")
        seen.add(entry["id"])
    return parsed


def digest(criteria: Iterable[dict]) -> str:
    """Frozen at creation: editing the criteria later is a different task."""

    body = json.dumps(list(criteria), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def checks(criteria: Iterable[dict]) -> list[dict]:
    return [entry for entry in criteria if entry.get("class") == "check"]


def judges(criteria: Iterable[dict]) -> list[dict]:
    return [entry for entry in criteria if entry.get("class") == "judge"]


EMPTY = (
    "(none declared) - this task has no acceptance criteria. Treat that as a defect of "
    "the task, not as permission: say so in your summary instead of guessing."
)


def render(criteria: Iterable[dict], *, evidence_path: str | None = None) -> str:
    """Plain text for a brief or an agent prompt."""

    rows = list(criteria)
    if not rows:
        return EMPTY
    lines = []
    for entry in rows:
        note = f"  ({entry['note']})" if entry.get("note") else ""
        lines.append(f"  {entry['id']} [{entry['class']}] {entry['criterion']}{note}")
    if evidence_path:
        lines.append("")
        lines.append(f"  Write one line per criterion into {evidence_path} before you finish:")
        lines.append("    A1: <the command you ran> -> exit 0, <one line of output>")
        lines.append("    A2: judged - <why it holds, with file:line evidence>")
    return "\n".join(lines)
