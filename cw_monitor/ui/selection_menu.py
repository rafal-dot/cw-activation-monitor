"""Scrollable multi-choice draft selector with an explicit All option."""

import curses


class SelectionMenu:
    def __init__(self, selection: frozenset[str] | None, options: tuple[str, ...], title: str, all_label: str):
        self.options, self.title, self.all_label = options, title, all_label
        self.selection = selection
        self.cursor = 0 if selection is None else next((i + 1 for i, b in enumerate(options) if b in selection), 0)
        self.closed = False
        self.cancelled = False
        self.error = ""

    def handle(self, key: int) -> None:
        if key == 27:
            self.closed = self.cancelled = True
        elif key in (10, 13, curses.KEY_ENTER):
            if self.selection == frozenset():
                self.error = "Select an option or All."
            else:
                self.closed = True
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            delta = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: -5, curses.KEY_NPAGE: 5}[key]
            self.cursor = max(0, min(len(self.options), self.cursor + delta))
        elif key in (ord("a"), ord("A")):
            self.selection, self.cursor, self.error = None, 0, ""
        elif key == ord(" "):
            if self.cursor == 0:
                self.selection = None
            else:
                band = self.options[self.cursor - 1]
                selected = self.selection or frozenset()
                self.selection = selected - {band} if band in selected else selected | {band}
            self.error = ""

    def overlay(self, frame: list[tuple[str, str]], height: int, width: int) -> list[tuple[str, str]]:
        if height < 8 or width < 40:
            return frame
        capacity = min(1 + len(self.options), height - 3)
        start = max(0, min(self.cursor - capacity // 2, 1 + len(self.options) - capacity))
        lines = [(self.title + ": Space toggles; a=all", "title")]
        choices = (self.all_label, *self.options)
        for i in range(start, start + capacity):
            checked = self.selection is None if i == 0 else self.selection is not None and choices[i] in self.selection
            lines.append((f"{'>' if i == self.cursor else ' '} [{'x' if checked else ' '}] {choices[i]}",
                          "table_header" if i == self.cursor else "normal"))
        lines.extend([(self.error or "Arrows/PageUp/PageDown: move", "error" if self.error else "muted"),
                      ("Enter=keep Esc=cancel q=quit", "muted")])
        result = list(frame)
        offset = max(0, (height - len(lines)) // 2)
        result[offset:offset + len(lines)] = lines
        return result
