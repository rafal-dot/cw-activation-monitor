"""Colour policy and semantic spans shared by the terminal views."""

from __future__ import annotations

import os
import re
import sys


PROGRAM_COLORS = {"POTA": "yellow", "WWFF": "green", "SOTA": "blue"}
COLOR_FIELDS = {5: "snr", 6: "wpm", 15: "program"}
ANSI_FOREGROUNDS = {"red": "31", "yellow": "33", "green": "32", "blue": "94"}


def colors_enabled(mode: str, stream=None) -> bool:
    if mode != "auto":
        return mode == "always"
    stream = sys.stdout if stream is None else stream
    return stream.isatty() and os.environ.get("TERM", "dumb") != "dumb" and "NO_COLOR" not in os.environ


def value_spans(field: int, text: str, snr_limits: tuple[int, int],
                wpm_limits: tuple[int, int]) -> list[tuple[int, int, str]]:
    """Colour each receiver's value independently, preserving receiver order."""
    kind = COLOR_FIELDS.get(field)
    if kind == "program":
        token = text.strip()
        if token in PROGRAM_COLORS:
            begin = text.index(token)
            return [(begin, begin + len(token), PROGRAM_COLORS[token])]
    if kind not in ("snr", "wpm"):
        return []
    spans = []
    for token in re.finditer(r"[+-]?\d+", text):
        value = int(token.group())
        if kind == "snr":
            color = "red" if value < snr_limits[0] else "yellow" if value < snr_limits[1] else "green"
        else:
            color = "green" if value <= wpm_limits[0] else "yellow" if value <= wpm_limits[1] else "red"
        spans.append((token.start(), token.end(), color))
    return spans


def measurement_spans(text: str, snr_limits: tuple[int, int],
                      wpm_limits: tuple[int, int]) -> list[tuple[int, int, str]]:
    """Retain signal colours when narrow layouts move measurements below rows."""
    spans = []
    for match in re.finditer(r"(SNR dB|WPM):\s*([+-]?\d+(?:/[+-]?\d+)*)", text):
        field = 5 if match[1] == "SNR dB" else 6
        spans.extend((match.start(2) + begin, match.start(2) + end, color)
                     for begin, end, color in value_spans(field, match[2], snr_limits, wpm_limits))
    return spans


def ansi_text(text: str, style: str = "normal") -> str:
    """Apply ANSI only after layout; NEW and changed cells override column colours."""
    attributes = ["2" if style == "history" else ""] * len(text)
    if style != "new":
        for begin, end, color in getattr(text, "color_spans", ()):
            for index in range(max(0, begin), min(len(text), end)):
                attributes[index] = ("2;" if style == "history" else "") + ANSI_FOREGROUNDS[color]
    else:
        attributes = ["30;43"] * len(text)
    for begin, end in getattr(text, "highlights", ()):
        for index in range(max(0, begin), min(len(text), end)):
            attributes[index] = "4;30;47"
    result, previous = [], ""
    for char, attribute in zip(text, attributes):
        if attribute != previous:
            result.append("\x1b[" + (attribute or "0") + "m")
            previous = attribute
        result.append(char)
    if previous:
        result.append("\x1b[0m")
    return "".join(result)
