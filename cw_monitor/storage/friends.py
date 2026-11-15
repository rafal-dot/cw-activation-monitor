"""Portable friend CSV and atomic, separate alert-deduplication history."""

from __future__ import annotations

from dataclasses import asdict
import csv
import io
import json
import math
import os
from pathlib import Path
import tempfile

from ..config import CALL_PATTERN
from ..friends import Friend, FriendSeen, MAX_FRIENDS
from ..sources.http import strict_json
from .files import lock_csv

FIELDS = ("callsign", "first_name", "last_name", "comment")
MAX_BYTES = 2_000_000
MAX_HISTORY_BYTES = 8_000_000


def decode_friends(text: str) -> list[Friend]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Friends CSV exceeds 2 MB")
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    rows = []
    for row in reader:
        if row and row != [""]:
            rows.append(row)
        if len(rows) > MAX_FRIENDS + 1:
            raise ValueError(f"At most {MAX_FRIENDS} friends are supported")
    if not rows or len(rows[0]) != len(FIELDS) or set(rows[0]) != set(FIELDS):
        raise ValueError("CSV header must contain callsign,first_name,last_name,comment")
    if len(rows) > MAX_FRIENDS + 1:
        raise ValueError(f"At most {MAX_FRIENDS} friends are supported")
    indexes = [rows[0].index(field) for field in FIELDS]
    result, seen = [], set()
    for number, row in enumerate(rows[1:], 2):
        try:
            if len(row) != len(FIELDS):
                raise ValueError("Expected four fields")
            call, first, last, comment = (row[index] for index in indexes)
            friend = Friend(call.strip().upper(), first.strip(), last.strip(), comment.replace("\r\n", "\n").replace("\r", "\n")).validate()
            if friend.call in seen:
                raise ValueError("Duplicate friend callsign")
            seen.add(friend.call)
            result.append(friend)
        except ValueError as exc:
            raise ValueError(f"CSV record {number}: {exc}") from exc
    return result


def read_friends(path: Path) -> list[Friend]:
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Friends CSV exceeds 2 MB")
    return decode_friends(data.decode("utf-8-sig"))


def encode_friends(friends: list[Friend]) -> str:
    if len(friends) > MAX_FRIENDS or len({friend.call for friend in friends}) != len(friends):
        raise ValueError("Too many or duplicate friends")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(FIELDS)
    for friend in friends:
        friend.validate()
        writer.writerow((friend.call, friend.first_name, friend.last_name, friend.comment))
    return output.getvalue()


class FriendHistory:
    def __init__(self, path: Path):
        self.path = path
        self.error = ""
        self.saved = []
        self.lock = lock_csv(path, "Friends history")

    def read(self):
        try:
            with self.path.open("rb") as source:
                data = source.read(MAX_HISTORY_BYTES + 1)
            if len(data) > MAX_HISTORY_BYTES:
                raise ValueError("Friends history exceeds 8 MB")
            payload = strict_json(data)
            if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("seen"), list):
                raise ValueError("Invalid Friends history format")
            if len(payload["seen"]) > 10000:
                raise ValueError("Friends history is too large")
            records = []
            for row in payload["seen"]:
                record = FriendSeen(**row)
                if (not CALL_PATTERN.fullmatch(record.call) or not isinstance(record.band, str) or not 1 <= len(record.band) <= 64
                        or not isinstance(record.alerted, bool)
                        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 253402300799.999
                               for v in (record.first_seen, record.last_seen))):
                    raise ValueError("Invalid Friends history record")
                records.append(record)
            if len({r.key for r in records}) != len(records):
                raise ValueError("Duplicate Friends history record")
            self.saved = sorted(records, key=lambda row: row.key)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self.error = f"Cannot restore Friends history: {exc}"
        return self.saved

    def write(self, records):
        if records == self.saved:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".friends-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump({"version": 1, "seen": [asdict(r) for r in records]}, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.saved = list(records)
            self.error = ""
        except (OSError, ValueError) as exc:
            self.error = f"Cannot save Friends history: {exc}"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def close(self):
        self.lock.close()
