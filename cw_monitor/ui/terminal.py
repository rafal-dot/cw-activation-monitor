"""Incremental curses drawing without clearing the screen on each fetch."""

import curses
import unicodedata


# Stable foreground colours: xterm-256 index and an eight-colour fallback.
# Common HF bands come first when the terminal has few available colour pairs.
BAND_COLORS = {
    "160m": (216, curses.COLOR_YELLOW), "80m": (183, curses.COLOR_MAGENTA),
    "60m": (219, curses.COLOR_WHITE), "40m": (117, curses.COLOR_CYAN),
    "30m": (121, curses.COLOR_YELLOW), "20m": (84, curses.COLOR_GREEN),
    "17m": (228, curses.COLOR_WHITE), "15m": (215, curses.COLOR_YELLOW),
    "12m": (213, curses.COLOR_CYAN), "10m": (207, curses.COLOR_MAGENTA),
    "6m": (153, curses.COLOR_CYAN), "2m": (195, curses.COLOR_GREEN),
    "4m": (159, curses.COLOR_GREEN), "70cm": (223, curses.COLOR_MAGENTA),
    "2190m": (146, curses.COLOR_WHITE), "630m": (151, curses.COLOR_GREEN),
    "560m": (152, curses.COLOR_CYAN), "8m": (154, curses.COLOR_YELLOW),
    "5m": (193, curses.COLOR_WHITE), "1.25m": (123, curses.COLOR_CYAN),
    "33cm": (229, curses.COLOR_YELLOW), "23cm": (225, curses.COLOR_MAGENTA),
    "13cm": (158, curses.COLOR_GREEN), "9cm": (217, curses.COLOR_MAGENTA),
    "6cm": (222, curses.COLOR_YELLOW), "3cm": (186, curses.COLOR_YELLOW),
    "1.25cm": (189, curses.COLOR_CYAN), "6mm": (194, curses.COLOR_GREEN),
    "4mm": (224, curses.COLOR_MAGENTA), "2.5mm": (185, curses.COLOR_YELLOW),
    "2mm": (230, curses.COLOR_WHITE), "1mm": (120, curses.COLOR_GREEN),
    "submm": (252, curses.COLOR_WHITE),
}


def fit_cells(text: str, width: int) -> str:
    """Clip and pad a row, accounting for wide and combining characters."""
    result, used = [], 0
    for char in text:
        if unicodedata.category(char).startswith("C"):
            continue
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result) + " " * (width - used)


class CursesDisplay:
    def __init__(self, screen, colors: bool = True):
        self.screen = screen
        self.previous: list[tuple[str, str, tuple, tuple, tuple, tuple]] = []
        self.band_attributes: dict[str, int] = {}
        self.value_attributes: dict[str, int] = {}
        self.size = None
        self.attributes = {"normal": 0, "title": curses.A_BOLD,
                           "banner": curses.A_REVERSE | curses.A_BOLD,
                           "new": curses.A_REVERSE | curses.A_BOLD,
                           "updated": curses.A_BOLD,
                           "updated_cell": curses.A_BOLD | curses.A_REVERSE | curses.A_UNDERLINE,
                           "history": curses.A_DIM, "muted": curses.A_DIM,
                           "table_header": curses.A_BOLD, "separator": curses.A_DIM,
                           "error": curses.A_BOLD}
        screen.timeout(200)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if colors and curses.has_colors():
            curses.start_color()
            # Explicit backgrounds also work without the default_colors extension.
            for number, foreground, background in ((1, curses.COLOR_BLACK, curses.COLOR_YELLOW),
                                                    (2, curses.COLOR_CYAN, curses.COLOR_BLACK),
                                                    (3, curses.COLOR_RED, curses.COLOR_BLACK),
                                                    (4, curses.COLOR_BLACK, curses.COLOR_WHITE),
                                                    (5, curses.COLOR_WHITE, curses.COLOR_BLACK)):
                curses.init_pair(number, foreground, background)
            self.attributes["title"] |= curses.color_pair(2)
            # Bold black text can map to grey/white in terminal themes.
            self.attributes["banner"] = curses.color_pair(1)
            self.attributes["new"] = curses.color_pair(1)
            self.attributes["error"] |= curses.color_pair(3)
            self.attributes["updated"] = curses.A_BOLD | curses.color_pair(2)
            self.attributes["updated_cell"] = curses.A_UNDERLINE | curses.color_pair(4)
            self.attributes["separator"] |= curses.color_pair(5)
            self.init_band_colors()

    def init_band_colors(self) -> None:
        colors = getattr(curses, "COLORS", 8)
        limit = getattr(curses, "COLOR_PAIRS", 8)
        pairs = {curses.COLOR_CYAN: curses.color_pair(2), curses.COLOR_WHITE: curses.color_pair(5),
                 curses.COLOR_RED: curses.color_pair(3)}
        next_pair = 6

        def foreground(color):
            nonlocal next_pair
            if color in pairs:
                return pairs[color]
            if color >= colors or next_pair >= limit:
                return None
            try:
                curses.init_pair(next_pair, color, curses.COLOR_BLACK)
                pairs[color] = curses.color_pair(next_pair)
            except curses.error:
                return None
            next_pair += 1
            return pairs[color]

        for color in (curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA):
            foreground(color)
        for name, preferred, fallback in (("red", 203, curses.COLOR_RED), ("yellow", 228, curses.COLOR_YELLOW),
                                           ("green", 84, curses.COLOR_GREEN), ("blue", 117, curses.COLOR_BLUE)):
            attribute = foreground(preferred) if colors >= 256 else None
            if attribute is None:
                attribute = pairs.get(fallback, pairs[curses.COLOR_CYAN]
                                                if name == "blue" else pairs[curses.COLOR_WHITE])
                if name == "blue":
                    attribute |= curses.A_BOLD  # Keep blue legible on a black background.
            self.value_attributes[name] = attribute
        for band, (preferred, fallback) in BAND_COLORS.items():
            attribute = foreground(preferred) if colors >= 256 else None
            if attribute is None:
                attribute = pairs.get(fallback, pairs[curses.COLOR_WHITE])
            self.band_attributes[band] = attribute

    def draw(self, lines: list[tuple[str, str]]) -> None:
        height, width = self.screen.getmaxyx()
        if self.size != (height, width):
            self.previous = []
            if self.size is not None:
                self.screen.erase()  # Only resize requires a complete redraw.
            self.size = height, width
        # Leave the last column free to avoid automatic terminal wrapping.
        frame = [(fit_cells(text, max(0, width - 1)), style, getattr(text, "highlights", ()),
                  getattr(text, "separators", ()), getattr(text, "band_spans", ()),
                  getattr(text, "color_spans", ()))
                 for text, style in lines[:height]]
        frame += [(" " * max(0, width - 1), "normal", (), (), (), ())] * (height - len(frame))

        def attributes(text, style, highlights, separators, band_spans, color_spans):
            result = [self.attributes.get(style, 0)] * len(text)
            if style in ("normal", "history"):
                for begin, end, band in band_spans:
                    if band in self.band_attributes:
                        attribute = self.attributes.get(style, 0) | self.band_attributes[band]
                        for index in range(max(0, begin), min(len(text), end)):
                            result[index] = attribute
            if style in ("normal", "history", "muted"):
                for begin, end, color in color_spans:
                    if color in self.value_attributes:
                        attribute = self.attributes.get(style, 0) | self.value_attributes[color]
                        for index in range(max(0, begin), min(len(text), end)):
                            result[index] = attribute
            for begin, end in separators:
                for index in range(max(0, begin), min(len(text), end)):
                    result[index] = self.attributes["separator"]
            for begin, end in highlights:
                for index in range(max(0, begin), min(len(text), end)):
                    result[index] = self.attributes["updated_cell"]
            return result

        def column(text):
            return sum(0 if unicodedata.combining(c) else
                       2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)

        changed = False
        for row, (text, style, highlights, separators, band_spans, color_spans) in enumerate(frame):
            previous = self.previous[row] if row < len(self.previous) else None
            if previous == (text, style, highlights, separators, band_spans, color_spans):
                continue
            attrs = attributes(text, style, highlights, separators, band_spans, color_spans)
            start, end = 0, len(text)
            # Repaint changed cells; row styling changes only on lifecycle events.
            if previous and all(not unicodedata.combining(c) and unicodedata.east_asian_width(c) not in "WF"
                                for c in text + previous[0]):
                old = previous[0]
                old_attrs = attributes(*previous)
                while (start < min(len(old), len(text)) and old[start] == text[start]
                       and old_attrs[start] == attrs[start]):
                    start += 1
                while (end > start and end <= len(old) and old[end - 1] == text[end - 1]
                       and old_attrs[end - 1] == attrs[end - 1]):
                    end -= 1
            if end > start:
                try:
                    position = start
                    while position < end:
                        stop = position + 1
                        while stop < end and attrs[stop] == attrs[position]:
                            stop += 1
                        self.screen.addstr(row, column(text[:position]), text[position:stop], attrs[position])
                        position = stop
                except curses.error:
                    # The terminal can shrink between getmaxyx and addstr.
                    self.previous, self.size = [], None
                    return
                changed = True
        self.previous = frame
        if changed:
            self.screen.noutrefresh()
            curses.doupdate()

    @staticmethod
    def bell() -> None:
        curses.beep()
