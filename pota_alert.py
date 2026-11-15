#!/usr/bin/env python3
"""Compatibility entry point for CW Hunter; prefer cw_hunter.py."""

from cw_monitor.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
