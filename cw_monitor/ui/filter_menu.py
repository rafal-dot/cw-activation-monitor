"""Editable threshold overlay without pausing the monitor worker."""

import curses
from ..filtering import SnrFilter
from .band_menu import BandMenu
from .selection_menu import SelectionMenu
from .band_snr_menu import BandSnrMenu
from ..snr import validate_bounds


class FilterMenu:
    def __init__(self, value: SnrFilter, spotters: list[str] | tuple[str, ...] = ()):
        self.text = "" if value.minimum is None else str(value.minimum)
        self.max_text = "" if value.maximum is None else str(value.maximum)
        self.upper = False
        self.show_filtered = value.show_filtered
        self.bands = value.bands
        self.spotters = value.spotters
        self.monitored_spotters = tuple(spotters)
        self.per_band = dict(value.per_band)
        self.snr_menu = None
        self.spotter_menu = None
        self.band_menu = None
        self.replace = True
        self.closed = False
        self.error = ""

    def handle(self, key: int) -> SnrFilter | None:
        if self.snr_menu is not None:
            self.snr_menu.handle(key)
            if self.snr_menu.closed:
                if not self.snr_menu.cancelled:
                    self.per_band = dict(self.snr_menu.rules)
                self.snr_menu = None
            return None
        if self.spotter_menu is not None:
            self.spotter_menu.handle(key)
            if self.spotter_menu.closed:
                if not self.spotter_menu.cancelled:
                    self.spotters = self.spotter_menu.selection
                self.spotter_menu = None
            return None
        if self.band_menu is not None:
            self.band_menu.handle(key)
            if self.band_menu.closed:
                if not self.band_menu.cancelled:
                    self.bands = self.band_menu.selection
                self.band_menu = None
            return None
        if key in (27, ord("f"), ord("F")):
            self.closed = True
        elif key in (10, 13, curses.KEY_ENTER):
            try:
                minimum = int(self.text) if self.text else None
                maximum = int(self.max_text) if self.max_text else None
                if minimum is not None and maximum is not None and minimum > maximum:
                    self.error = "Minimum must not exceed maximum."
                    return None
                result = SnrFilter(minimum, self.show_filtered, self.bands, self.spotters, maximum, self.per_band)
                self.closed = True
                return result
            except ValueError:
                self.error = "Enter whole dB from -100 to 100."
        elif key in (9, curses.KEY_UP, curses.KEY_DOWN, ord("l"), ord("L"), ord("u"), ord("U")):
            self.upper = key in (ord("u"), ord("U")) if key in (ord("l"), ord("L"), ord("u"), ord("U")) else not self.upper
            self.replace, self.error = True, ""
        elif key in (ord("o"), ord("O")):
            self._set_text("")
            self.error = ""
            self.replace = False
        elif key in (ord("x"), ord("X")):
            try:
                minimum = int(self.text) if self.text else None
                maximum = int(self.max_text) if self.max_text else None
                validate_bounds(minimum, maximum)
                self.snr_menu = BandSnrMenu(self.per_band, minimum, maximum)
            except ValueError:
                self.error = "Fix the global bounds first."
        elif key in (ord("v"), ord("V")):
            self.show_filtered = not self.show_filtered
        elif key in (ord("b"), ord("B")):
            self.band_menu = BandMenu(self.bands)
        elif key in (ord("p"), ord("P")):
            self.spotter_menu = SelectionMenu(self.spotters, self.monitored_spotters, "Spotters", "All monitored spotters")
        elif key in (8, 127, curses.KEY_BACKSPACE):
            self._set_text(self._text()[:-1])
            self.replace = False
        elif key in [ord(c) for c in "-0123456789"]:
            self._set_text(chr(key) if self.replace else (self._text() + chr(key))[:5])
            self.replace, self.error = False, ""
        return None

    def _text(self) -> str:
        return self.max_text if self.upper else self.text

    def _set_text(self, text: str) -> None:
        if self.upper:
            self.max_text = text
        else:
            self.text = text

    def overlay(self, frame: list[tuple[str, str]], height: int, width: int) -> list[tuple[str, str]]:
        if self.snr_menu is not None:
            return self.snr_menu.overlay(frame, height, width)
        if self.spotter_menu is not None:
            return self.spotter_menu.overlay(frame, height, width)
        if self.band_menu is not None:
            return self.band_menu.overlay(frame, height, width)
        if height < 8 or width < 40:
            return frame
        bands = "All bands" if self.bands is None else ",".join(sorted(self.bands))
        if len(bands) > width - 9:
            bands = f"{len(self.bands)} selected"
        spotters = "All monitored" if self.spotters is None else ",".join(sorted(self.spotters))
        if len(spotters) > width - 10:
            spotters = f"{len(self.spotters)} selected"
        count = sum(r.enabled for r in self.per_band.values())
        lines = [("Filters" + (f" | SNR bands: {count}" if count else ""), "title"),
                 (("  " if self.upper else "> ") + "Minimum dB: " + (self.text or "Off"), "normal" if self.upper else "table_header"),
                 (("> " if self.upper else "  ") + "Maximum dB: " + (self.max_text or "Off"), "table_header" if self.upper else "normal"),
                 ("Bands: " + bands, "normal"),
                 ("Spotters: " + spotters, "normal"),
                 (f"[{'x' if self.show_filtered else ' '}] Show filtered (no alerts)", "normal"),
                 (self.error or "Tab/o/v b/p:lists x:band SNR", "error" if self.error else "muted"),
                 ("Enter=apply Esc/f=cancel q=quit", "muted")]
        start = max(0, (height - len(lines)) // 2)
        indent = " " * min(4, max(0, (width - 35) // 2))
        result = list(frame)
        result[start:start + len(lines)] = [(indent + text, style) for text, style in lines]
        return result
