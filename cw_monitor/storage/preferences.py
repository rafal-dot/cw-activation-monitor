"""Small, atomic user-interface preferences, separate from activation logs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from ..sorting import SortOrder
from ..filtering import SnrFilter


def sort_settings_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config) if config and Path(config).is_absolute() else Path.home() / ".config"
    return root / "cw-activation-monitor" / "sort.json"


def load_sort(path: Path | None = None) -> tuple[SortOrder, str]:
    path = path or sort_settings_path()
    try:
        with path.open(encoding="utf-8-sig") as source:
            text = source.read(65_537)
        if len(text) > 65_536:
            raise ValueError("preferences exceed 64 KB")
        item = json.loads(text)
        if not isinstance(item, dict):
            raise ValueError("expected a JSON object")
        return SortOrder(item.get("field", "last"), item.get("direction", "desc")), ""
    except FileNotFoundError:
        return SortOrder(), ""
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return SortOrder(), f"Cannot read sort settings ({path}): {exc}"


def _save_settings(item: dict, path: Path, label: str) -> str:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".preferences-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(item, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return ""
    except OSError as exc:
        return f"Cannot save {label} settings ({path}): {exc}"
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # A cleanup failure must not hide the original save error.


def save_sort(order: SortOrder, path: Path | None = None) -> str:
    return _save_settings({"field": order.field, "direction": order.direction}, path or sort_settings_path(), "sort")


def filter_settings_path() -> Path:
    return sort_settings_path().with_name("filter.json")


def load_filter(path: Path | None = None) -> tuple[SnrFilter, str]:
    path = path or filter_settings_path()
    try:
        with path.open(encoding="utf-8-sig") as source:
            text = source.read(65_537)
        if len(text) > 65_536:
            raise ValueError("preferences exceed 64 KB")
        item = json.loads(text)
        if not isinstance(item, dict):
            raise ValueError("expected a JSON object")
        return SnrFilter(item.get("minimum"), item.get("show_filtered", False)), ""
    except FileNotFoundError:
        return SnrFilter(), ""
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return SnrFilter(), f"Cannot read filter settings ({path}): {exc}"


def save_filter(value: SnrFilter, path: Path | None = None) -> str:
    return _save_settings({"minimum": value.minimum, "show_filtered": value.show_filtered},
                          path or filter_settings_path(), "filter")


def configure_filter(args) -> None:
    saved, args.filter_error = load_filter()
    value = SnrFilter(args.min_snr if args._min_snr_explicit else saved.minimum,
                      args.show_filtered if args._show_filtered_explicit else saved.show_filtered)
    args.min_snr, args.show_filtered = value.minimum, value.show_filtered
    if args._min_snr_explicit or args._show_filtered_explicit:
        args.filter_error = save_filter(value)


def configure_sort(args) -> None:
    saved, args.sort_error = load_sort()
    explicit = args.sort is not None or args.sort_order is not None
    selected = SortOrder.resolve(args.sort, args.sort_order, saved)
    args.sort, args.sort_order = selected.field, selected.direction
    if explicit:
        args.sort_error = save_sort(selected)
