"""Editable threshold overlay without pausing the monitor worker."""

import curses
from ..filtering import SnrFilter


class FilterMenu:
    def __init__(self, value: SnrFilter):
        self.text = "" if value.minimum is None else str(value.minimum)
        self.show_filtered = value.show_filtered
        self.replace = True
        self.closed = False
        self.error = ""

    def handle(self, key: int) -> SnrFilter | None:
        if key in (27, ord("f"), ord("F")):
            self.closed = True
        elif key in (10, 13, curses.KEY_ENTER):
            try:
                result = SnrFilter(int(self.text) if self.text else None, self.show_filtered)
                self.closed = True
                return result
            except ValueError:
                self.error = "Enter whole dB from -100 to 100."
        elif key in (ord("o"), ord("O")):
            self.text, self.error = "", ""
            self.replace = False
        elif key in (ord("v"), ord("V")):
            self.show_filtered = not self.show_filtered
        elif key in (8, 127, curses.KEY_BACKSPACE):
            self.text, self.replace = self.text[:-1], False
        elif key in [ord(c) for c in "-0123456789"]:
            self.text = chr(key) if self.replace else (self.text + chr(key))[:5]
            self.replace, self.error = False, ""
        return None

    def overlay(self, frame: list[tuple[str, str]], height: int, width: int) -> list[tuple[str, str]]:
        if height < 8 or width < 40:
            return frame
        lines = [("Minimum SNR filter", "title"),
                 ("Minimum dB: " + (self.text or "Off"), "table_header"),
                 (f"[{'x' if self.show_filtered else ' '}] Show filtered (no alerts)", "normal"),
                 ("One spotter at/above the limit.", "normal"),
                 (self.error or "Type dB; o=off; v=show/hide", "error" if self.error else "muted"),
                 ("Enter=apply Esc/f=cancel q=quit", "muted")]
        start = max(0, (height - len(lines)) // 2)
        indent = " " * min(4, max(0, (width - 35) // 2))
        result = list(frame)
        result[start:start + len(lines)] = [(indent + text, style) for text, style in lines]
        return result
