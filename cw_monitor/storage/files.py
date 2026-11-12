"""Atomic CSV schema upgrades and backup handling."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import shutil
import tempfile
from typing import TextIO


def lock_csv(path: Path, label: str) -> TextIO:
    """Hold a sidecar lock across atomic replacements of the data file."""
    import fcntl

    handle = path.with_name("." + path.name + ".lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError(f"{label} is already in use: {path}") from exc
    except BaseException:
        handle.close()
        raise
    return handle


def upgrade_csv_schema(path: Path, fields: tuple[str, ...]) -> None:
    """Under the caller's sidecar lock, upgrade known schemas with an exact backup.

    Supports the former receivers column and schemas without reference QTH.
    Unknown headers and malformed rows must leave the original file untouched.
    """
    if not path.exists() or not path.stat().st_size:
        return
    original = path.stat()
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, strict=True)
        header = tuple(reader.fieldnames or ())
        if header == fields:
            return
        rename_spotters = "spotters" in fields and "receivers" in header
        renamed = tuple("spotters" if name == "receivers" and rename_spotters else name for name in header)
        if renamed not in (fields, tuple(name for name in fields if name != "qth")):
            raise ValueError(f"File has a different CSV header: {path}")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix="." + path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"Wrong column count in {path}, line {reader.line_num}")
                    writer.writerow({name: row.get("receivers" if name == "spotters" and rename_spotters else name, "")
                                     for name in fields})
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, original.st_mode & 0o777)
            current = path.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != (original.st_ino, original.st_size, original.st_mtime_ns):
                raise ValueError(f"CSV changed during upgrade: {path}")
            backup_dir = path.parent / ".csv-backups"
            backup_dir.mkdir(exist_ok=True)
            prefix = "before-spotters-" if rename_spotters else "before-qth-"
            backup = Path(tempfile.mkdtemp(prefix=prefix, dir=backup_dir)) / path.name
            shutil.copy2(path, backup)
            if path.stat().st_mtime_ns != original.st_mtime_ns or path.stat().st_size != original.st_size:
                raise ValueError(f"CSV changed during upgrade: {path}")
            os.replace(temporary, path)
        except csv.Error as exc:
            raise ValueError(f"Invalid CSV file {path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
