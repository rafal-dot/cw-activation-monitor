"""Scrollable band SNR drafts; no preference or network effects until the parent applies."""

import curses
from dataclasses import replace
from ..snr import COMMON_BANDS, BandSnrRule, bounds_label
from .layout import fit_column


class BandSnrMenu:
    def __init__(self, rules, minimum, maximum):
        self.rules = dict(rules)
        self.global_label = bounds_label(minimum, maximum)
        self.cursor = 0
        self.editing = False
        self.own = False
        self.lower = self.upper = ""
        self.edit_upper = False
        self.replace = True
        self.error = ""
        self.closed = self.cancelled = False

    @property
    def band(self):
        return COMMON_BANDS[self.cursor]

    def handle(self, key):
        if self.editing:
            self._edit(key)
        elif key == 27:
            self.closed = self.cancelled = True
        elif key in (ord("a"), ord("A")):
            self.closed = True
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            delta = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -5, curses.KEY_NPAGE: 5}[key]
            self.cursor = min(len(COMMON_BANDS) - 1, max(0, self.cursor + delta))
        elif key == ord(" "):
            old = self.rules.get(self.band, BandSnrRule())
            self.rules[self.band] = replace(old, enabled=not old.enabled)
        elif key in (ord("g"), ord("G")):
            self.rules = {band: replace(rule, enabled=False) for band, rule in self.rules.items()}
        elif key in (10, 13, curses.KEY_ENTER):
            old = self.rules.get(self.band, BandSnrRule())
            self.own, self.lower, self.upper = old.enabled, "" if old.minimum is None else str(old.minimum), "" if old.maximum is None else str(old.maximum)
            self.edit_upper, self.replace, self.editing, self.error = False, True, True, ""

    def _edit(self, key):
        if key == 27:
            self.editing = False
        elif key in (10, 13, curses.KEY_ENTER):
            try:
                rule = BandSnrRule(self.own, int(self.lower) if self.lower else None, int(self.upper) if self.upper else None)
                self.rules[self.band] = rule
                self.editing = False
            except ValueError:
                if not self.own:
                    self.rules[self.band] = replace(self.rules.get(self.band, BandSnrRule()), enabled=False)
                    self.editing = False
                else:
                    self.error = "Use -100..100; minimum <= maximum."
        elif key in (ord("e"), ord("E")):
            self.own = not self.own
        elif key in (9, curses.KEY_UP, curses.KEY_DOWN, ord("l"), ord("L"), ord("u"), ord("U")):
            self.edit_upper = key in (ord("u"), ord("U")) if key in (ord("l"), ord("L"), ord("u"), ord("U")) else not self.edit_upper
            self.replace = True
        elif self.own:
            text = self.upper if self.edit_upper else self.lower
            if key in (ord("o"), ord("O")):
                text = ""
            elif key in (8, 127, curses.KEY_BACKSPACE):
                text = text[:-1]
            elif key in [ord(c) for c in "-0123456789"]:
                text = chr(key) if self.replace else (text + chr(key))[:5]
            else:
                return
            if self.edit_upper:
                self.upper = text
            else:
                self.lower = text
            self.replace, self.error = False, ""

    def overlay(self, frame, height, width):
        if height < 8 or width < 40:
            return frame
        if self.editing:
            lines = [(f"{self.band} SNR", "title"),
                     (f"[{'x' if self.own else ' '}] Use band settings (e)", "normal"),
                     (("  " if self.edit_upper else "> ") + "Minimum: " + (self.lower or "Off"), "normal" if self.edit_upper else "table_header"),
                     (("> " if self.edit_upper else "  ") + "Maximum: " + (self.upper or "Off"), "table_header" if self.edit_upper else "normal"),
                     ("Replaces BOTH global bounds" if self.own else "Global: " + self.global_label, "muted"),
                     (self.error or "Both Off = no SNR restriction", "error" if self.error else "muted"),
                     ("Tab:bound o:off e:own/global", "muted"), ("Enter:keep Esc:cancel q:quit", "muted")]
        else:
            capacity = min(len(COMMON_BANDS), height - 4)
            start = max(0, min(self.cursor - capacity // 2, len(COMMON_BANDS) - capacity))
            lines = [("SNR by band", "title")]
            for i in range(start, start + capacity):
                rule = self.rules.get(COMMON_BANDS[i], BandSnrRule())
                label = "Own: " + bounds_label(rule.minimum, rule.maximum) if rule.enabled else "Global: " + self.global_label
                lines.append((f"{'>' if i == self.cursor else ' '} {COMMON_BANDS[i]:6} {label}", "table_header" if i == self.cursor else "normal"))
            lines.extend([("Arrows:move Space:own/global", "muted"), ("Enter:edit g:global on all bands", "muted"), ("a:keep Esc:cancel q:quit", "muted")])
        result = list(frame)
        start = max(0, (height - len(lines)) // 2)
        result[start:start + len(lines)] = [(fit_column(text, width), style) for text, style in lines]
        return result
