"""One-way export to an Obsidian vault.

ROS owns only the *managed block* of each note. Text the user writes outside it is preserved;
if the user edits inside the managed block, the checksum no longer matches and ROS writes a
side-by-side `.ros-conflict.md` instead of overwriting. Paths are confined to the vault (no
symlink or `..` escapes) and every write is atomic (temporary sibling + replace).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database, now_iso
from .monitor.drafts import slugify

ROOT = "ROS"
BEGIN = re.compile(r"<!-- ros:begin id=(?P<id>\S+) sha=(?P<sha>[0-9a-f]{16}) -->\n(?P<body>.*?)\n<!-- ros:end -->", re.S)


@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    dry_run: bool = False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _block(ros_id: str, body: str) -> str:
    return f"<!-- ros:begin id={ros_id} sha={_sha(body)} -->\n{body}\n<!-- ros:end -->"


def safe_path(vault: Path, relative: str) -> Path:
    """Resolve `relative` inside the vault, refusing traversal and symlinked components."""
    root = vault.resolve()
    target = (root / relative)
    if ".." in Path(relative).parts or Path(relative).is_absolute():
        raise ValueError(f"ruta fuera del vault: {relative}")
    probe = root
    for part in Path(relative).parts:
        probe = probe / part
        if probe.is_symlink():
            raise ValueError(f"enlace simbólico no permitido dentro del vault: {probe}")
    if not str(target.resolve()).startswith(str(root) + os.sep):
        raise ValueError(f"ruta fuera del vault: {relative}")
    return target


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ros-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _publish(db: Database, vault: Path, relative: str, ros_id: str, title: str, body: str, res: ExportResult) -> None:
    path = safe_path(vault, relative)
    block = _block(ros_id, body)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        m = next((m for m in BEGIN.finditer(current) if m.group("id") == ros_id), None)
        if m is None:
            new = current.rstrip() + "\n\n" + block + "\n"
        else:
            if _sha(m.group("body")) != m.group("sha"):
                conflict = path.with_name(path.stem + ".ros-conflict.md")
                if not res.dry_run:
                    _atomic_write(conflict, f"---\nros_id: {ros_id}\nros_conflict: true\n---\n\n{block}\n")
                res.conflicts.append(relative)
                return
            if m.group("body") == body:
                res.unchanged.append(relative)
                return
            new = current[:m.start()] + block + current[m.end():]
    else:
        new = f"---\nros_id: {ros_id}\ntitle: \"{title.replace(chr(34), chr(39))}\"\nros_exported: {now_iso()}\n---\n\n" \
              f"{block}\n\n## Notas\n\n"
    if not res.dry_run:
        _atomic_write(path, new)
        row = db.one("SELECT version FROM publications WHERE target='obsidian' AND ros_id=?", (ros_id,))
        db.execute("""INSERT INTO publications (target, ros_id, path, content_hash, version, updated_at)
                      VALUES ('obsidian',?,?,?,?,?) ON CONFLICT(target, ros_id) DO UPDATE SET path=excluded.path,
                      content_hash=excluded.content_hash, version=publications.version+1, updated_at=excluded.updated_at""",
                   (ros_id, relative, _sha(body), (row["version"] + 1) if row else 1, now_iso()))
    res.written.append(relative)


def export_vault(db: Database, vault: str | Path, *, dry_run: bool = False) -> ExportResult:
    vault = Path(vault).expanduser()
    if not vault.is_dir():
        raise ValueError(f"el vault no existe: {vault}")
    res = ExportResult(dry_run=dry_run)
    research = db.all("""SELECT id, objective, status, finished_at, report_md FROM runs
                         WHERE kind='research' AND report_md IS NOT NULL ORDER BY id""")
    for r in research:
        rel = f"{ROOT}/Research/{r['id']:04d} {slugify(r['objective'])[:50]}.md"
        _publish(db, vault, rel, f"research-{r['id']}", r["objective"], r["report_md"], res)
    watches = db.all("SELECT id, name, spec_json FROM watches ORDER BY name")
    for w in watches:
        for d in db.all("SELECT id, period_end, markdown FROM digests WHERE watch_id=? ORDER BY id", (w["id"],)):
            rel = f"{ROOT}/Watches/{w['name']}/{d['period_end'][:10]} digest {d['id']}.md"
            _publish(db, vault, rel, f"digest-{d['id']}", f"{w['name']} {d['period_end'][:10]}", d["markdown"], res)
    lines = ["# ROS", "", "## Investigaciones", ""]
    lines += [f"- [[{ROOT}/Research/{r['id']:04d} {slugify(r['objective'])[:50]}|#{r['id']} {r['objective'][:80]}]] "
              f"({r['status']})" for r in research]
    lines += ["", "## Seguimientos", ""]
    for w in watches:
        digests = db.all("SELECT id, period_end FROM digests WHERE watch_id=? ORDER BY id DESC LIMIT 10", (w["id"],))
        lines.append(f"- **{w['name']}**: " + ", ".join(
            f"[[{ROOT}/Watches/{w['name']}/{d['period_end'][:10]} digest {d['id']}|{d['period_end'][:10]}]]"
            for d in digests))
    _publish(db, vault, f"{ROOT}/Home.md", "home", "ROS", "\n".join(lines), res)
    return res
