"""RBN protocol, caching, coverage and adaptive polling."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import re
from threading import Event
import time
from urllib.parse import urlencode

from ..config import CALL_PATTERN, RBN_URL, RBN_HIGH_TRAFFIC_WARNING
from ..friends import FriendIndex
from ..models import RbnSpot, SourceError
from ..values import clean, frequency, receiver_call, integer_reading
from . import http


class RbnLoad:
    """Estimate rows required for a window; grow immediately and shrink slowly."""

    levels = (10, 20, 50, 100)

    def __init__(self):
        self.limit = 10
        self.average: float | None = None
        self.peaks: deque[float] = deque(maxlen=10)
        self.quiet = 0

    def estimate(self, seconds: float) -> int:
        rate = max((self.average or 0) * 1.5, max(self.peaks, default=0) * 1.2)
        needed = rate * max(0, seconds) / 60
        return next((n for n in self.levels if n >= needed), 100)

    def start(self, seconds: float) -> int:
        self.limit = max(self.limit, self.estimate(seconds))
        return self.limit

    def grow(self) -> int:
        self.limit = next((n for n in self.levels if n > self.limit), 100)
        self.quiet = 0
        return self.limit

    def observe(self, count: int, elapsed: float, next_window: float, complete: bool) -> None:
        if not complete:
            self.limit, self.quiet = 100, 0
            return  # A truncated count is a lower bound, never a low-traffic sample.
        if elapsed <= 0:
            return
        rate = count * 60 / elapsed
        self.average = rate if self.average is None else 0.3 * rate + 0.7 * self.average
        self.peaks.append(rate)
        wanted = self.estimate(next_window)
        if wanted > self.limit:
            self.limit, self.quiet = wanted, 0
        elif wanted < self.limit:
            self.quiet += 1
            if self.quiet >= 3:
                self.limit = self.levels[self.levels.index(self.limit) - 1]
                self.quiet = 0
        else:
            self.quiet = 0


@dataclass
class RbnStream:
    load: RbnLoad = field(default_factory=RbnLoad)
    watermark: float | None = None
    spots: dict[str, RbnSpot] = field(default_factory=dict)
    sampled_ids: set[str] = field(default_factory=set)
    covered_since: float | None = None
    covered_until: float | None = None
    interval: float = 60
    quiet_intervals: int = 0
    batch_cursor: int = 0
    last_page_end: float | None = None
    traffic_gap_until: float = 0


class RbnClient:
    """Read the HTTPS feed used by the RBN website.

    Validate this internal interface's version and schema rather than
    assuming fixed column positions or a permanent CW mode identifier.
    """

    def __init__(self, timeout: float, small_batches: Event | None = None, interval: float = 60):
        self.remaining_requests = 8
        self.budget_reported = False
        self.timeout = timeout
        self.version = ""
        self.fields: list[str] = []
        self.cw_mode = ""
        self.warnings: list[str] = []
        self.small_batches = small_batches if small_batches is not None else Event()
        self.streams: dict[tuple, RbnStream] = {}
        self.status = "RBN: waiting"
        self.diagnostic_status = ""
        self.request_log: list[str] = []
        self.base_interval = self.poll_interval = interval
        self.friend_index = FriendIndex()

    def adjust_interval(self, stream: RbnStream, count: int, span: float, complete: bool, max_age: float) -> None:
        minimum = min(15, self.base_interval)
        if count >= 100 and span < 60:
            desired = max(minimum, span * 0.7)
            while stream.interval > desired:
                stream.interval = max(minimum, stream.interval / 2)
            stream.quiet_intervals = 0
        elif complete and stream.interval < self.base_interval:
            candidate = min(self.base_interval, stream.interval * 2)
            rate = max((stream.load.average or 0) * 1.5, max(stream.load.peaks, default=0) * 1.2)
            if rate * min(max_age, candidate + 15) / 60 <= 100:
                stream.quiet_intervals += 1
                if stream.quiet_intervals >= 6:
                    stream.interval, stream.quiet_intervals = candidate, 0
            else:
                stream.quiet_intervals = 0
        else:
            stream.quiet_intervals = 0
        self.poll_interval = stream.interval

    def initialize(self) -> None:
        page = http.get_text(RBN_URL + "/main.php", self.timeout)
        version = re.search(r'RBN_version_hash\s*=\s*[\"\']([a-zA-Z0-9]+)[\"\']', page)
        meta = http.get_json(RBN_URL + "/spots.php?meta=1", self.timeout)
        required = {"de", "dx", "freq", "db", "wpm", "epoch", "mode"}
        if not version or not isinstance(meta, dict):
            raise SourceError("RBN: unrecognized website version or metadata")
        fields = meta.get("spot_fields")
        modes = meta.get("modes")
        if not isinstance(fields, list) or not required.issubset(fields) or not isinstance(modes, dict):
            raise SourceError("RBN: spot data schema has changed")
        cw_mode = next((str(k) for k, v in modes.items()
                        if isinstance(v, dict) and v.get("mode") == "cw"), None)
        if cw_mode is None:
            raise SourceError("RBN: CW mode missing from metadata")
        self.version, self.fields, self.cw_mode = version[1], fields, cw_mode

    def _request(self, calls: list[str] | None, receivers: list[str], max_age: float,
                 limit: int) -> tuple[dict[str, RbnSpot], int, int]:
        for attempt in range(2):
            if not self._has_budget():
                return {}, 100, 1
            if not self.version:
                self.initialize()
            params = {"h": self.version, "cde": ",".join(receivers),
                      "ma": math.ceil(max_age), "m": self.cw_mode, "s": 0, "r": limit}
            if calls is not None:
                params["cdx"] = ",".join(calls)
            self.remaining_requests -= 1
            data = http.get_json(RBN_URL + "/spots.php?" + urlencode(params), self.timeout)
            if not isinstance(data, dict):
                raise SourceError("RBN: expected a JSON object")
            if data.get("ver_h") != self.version:
                self.version = ""
                if attempt == 0:
                    continue
                raise SourceError("RBN: website version changed; retrying next cycle")
            if data.get("error") or data.get("impossible"):
                raise SourceError("RBN: " + clean(data.get("error") or data["impossible"]))
            raw = data.get("spots", {})
            if raw == []:  # Empty PHP maps can be encoded as lists.
                raw = {}
            if not isinstance(raw, dict):
                raise SourceError("RBN: unsupported spot list format")
            result, skipped = {}, 0
            for spot_id, values in raw.items():
                try:
                    if not isinstance(values, list) or len(values) != len(self.fields):
                        raise SourceError("RBN: spot row format has changed")
                    item = dict(zip(self.fields, values))
                    if str(item["mode"]) != self.cw_mode:
                        continue
                    call = str(item["dx"]).strip().upper()
                    receiver = receiver_call(str(item["de"]))
                    if (calls is not None and call not in calls and self.friend_index.find(call) is None) or receiver not in receivers:
                        continue
                    if not CALL_PATTERN.fullmatch(call) or not CALL_PATTERN.fullmatch(receiver) or isinstance(item["epoch"], bool):
                        raise ValueError("Invalid call or timestamp")
                    when = float(item["epoch"])
                    if not math.isfinite(when) or not 0 <= when <= 253402300799.999:
                        raise ValueError("Invalid timestamp")
                    result[str(spot_id)] = RbnSpot(call, receiver, frequency(item["freq"]), when,
                                                  integer_reading(item["db"], -100, 100), integer_reading(item["wpm"], 0, 200))
                except (KeyError, TypeError, ValueError, OverflowError):
                    skipped += 1
            if skipped:
                self.warnings.append(f"RBN: skipped {skipped} invalid spots")
            if raw and skipped == len(raw):
                raise SourceError("RBN: could not parse any spots")
            self.request_log.append(f"{'stream' if calls is None else 'batches'} "
                                    f"spotters={','.join(receivers)} calls={','.join(calls) if calls else 'all'} "
                                    f"limit={limit} received={len(raw)} skipped={skipped}")
            return result, len(raw), skipped
        raise AssertionError("Unreachable")

    def _has_budget(self) -> bool:
        if self.remaining_requests > 0:
            return True
        if not self.budget_reported:
            self.warnings.append("RBN: 8-request budget reached; remaining batches deferred; results may be incomplete")
        self.budget_reported = True
        return False

    def _query(self, calls: list[str], receivers: list[str], max_age: float) -> dict[str, RbnSpot]:
        if not self._has_budget():
            return {}
        result, count, skipped = self._request(calls, receivers, max_age, 100)
        if count >= 100:
            if len(calls) > 1:
                middle = len(calls) // 2
                result.update(self._query(calls[:middle], receivers, max_age))
                result.update(self._query(calls[middle:], receivers, max_age))
            elif len(receivers) > 1:
                for receiver in receivers:
                    result.update(self._query(calls, [receiver], max_age))
            else:
                self.warnings.append(f"RBN: 100-spot limit reached for {calls[0]} / {receivers[0]}; "
                                     "results may be incomplete even in smaller batches")
        return result

    def fetch(self, calls: list[str], receivers: list[str], max_age: float, *, friends=()) -> list[RbnSpot]:
        self.friend_index = FriendIndex(friends)
        calls = sorted(set(calls) | {friend.call for friend in friends})
        self.warnings = []
        self.request_log = []
        self.diagnostic_status = ""
        self.remaining_requests = 8
        self.budget_reported = False
        if not calls:
            self.poll_interval = self.base_interval
            self.status = "RBN: idle (no unconfirmed activations)"
            return []
        started = time.time()
        key = tuple(sorted(receivers)), max_age
        stream = self.streams.setdefault(key, RbnStream(interval=self.base_interval))
        if ((stream.watermark is not None and stream.watermark > started)
                or (stream.covered_until is not None and stream.covered_until > started)):
            stream = self.streams[key] = RbnStream(interval=self.base_interval)
        overlap = 15.0
        target = max(started - max_age, stream.watermark - overlap) if stream.watermark is not None else started - max_age
        window = max(1.0, started - target)
        previous = stream.watermark
        seen_before = set(stream.spots)
        previous_page_end = stream.last_page_end
        previous_read_at = stream.covered_until
        was_at_minimum = stream.interval <= min(15, self.base_interval)
        limit = stream.load.start(window)
        while True:
            rows, count, skipped = self._request(None, receivers, max_age, limit)
            now = time.time()
            stream.spots.update(rows)
            stream.spots = {i: s for i, s in stream.spots.items() if 0 <= now - s.time <= max_age}
            oldest = min((s.time for s in rows.values()), default=started)
            complete = not skipped and (count < limit or oldest < target)
            if complete or limit == 100:
                break
            limit = stream.load.grow()
        # Consecutive capped reads can cover new arrivals even while an older
        # gap remains. Only overlapping time ranges establish continuous coverage.
        if not skipped:
            covered_from = now - max_age if count < limit else oldest if rows else None
            if covered_from is not None:
                if (stream.covered_since is not None and stream.covered_until is not None
                        and covered_from <= stream.covered_until):
                    stream.covered_since = min(stream.covered_since, covered_from)
                else:
                    stream.covered_since = covered_from
                stream.covered_until = started
        complete = not skipped and (count < limit or (stream.covered_since is not None
                                                     and stream.covered_since < now - max_age))
        elapsed = min(max_age, started - previous) if previous is not None else max_age
        sample_start = max(started - max_age, previous) if previous is not None else started - max_age
        new_count = sum(i not in stream.sampled_ids and s.time >= sample_start for i, s in stream.spots.items())
        stream.load.observe(new_count, elapsed, min(max_age, max(1.0, elapsed) + overlap), complete)
        valid = {key: spot for key, spot in rows.items() if 0 <= now - spot.time <= max_age}
        newest = max((spot.time for spot in valid.values()), default=oldest)
        new_page = any(key not in seen_before for key in valid)
        # Delivery latency is not the arrival rate; unchanged pages are not new bursts.
        span = newest - min((spot.time for spot in valid.values()), default=newest)
        self.adjust_interval(stream, len(valid) if new_page else 0, span, complete, max_age)
        # Startup backfill is not a missed live update. A capped page with no
        # timestamp overlap at the fastest cadence can hide intervening calls.
        fast_cycle = previous_read_at is not None and 0 < started - previous_read_at <= 30
        if (was_at_minimum and fast_cycle and previous_page_end is not None and new_page
                and not skipped and len(valid) >= 100 and oldest > previous_page_end):
            stream.traffic_gap_until = now + max_age
        if not skipped and (count < limit or valid):
            end = started if count < limit else newest
            # Cached/out-of-order pages must not move the live checkpoint backwards.
            stream.last_page_end = max(previous_page_end if previous_page_end is not None else end, end)
        if complete:
            stream.traffic_gap_until = 0

        if complete:
            stream.watermark = started
            stream.sampled_ids = set(stream.spots)
        elif self.small_batches.is_set():
            ordered = sorted(set(calls) | {s.call for s in stream.spots.values() if self.friend_index.find(s.call) is not None})
            first = stream.batch_cursor % len(ordered)
            rotated = ordered[first:] + ordered[:first]
            groups = [rotated[start:start + 20] for start in range(0, len(ordered), 20)]
            for offset in range(len(groups)):
                if not self._has_budget():
                    break
                # Advance inside a group too: a saturated early call must not starve its neighbours.
                stream.batch_cursor = (first + offset * 20 + 1) % len(ordered)
                stream.spots.update(self._query(groups[offset], receivers, max_age))
        now = time.time()
        stream.spots = {i: s for i, s in stream.spots.items() if 0 <= now - s.time <= max_age}
        if stream.traffic_gap_until > now and not self.small_batches.is_set():
            self.warnings.append(RBN_HIGH_TRAFFIC_WARNING)
        mode = "batches" if self.small_batches.is_set() else "collecting recent receptions" if previous_page_end is None and not complete else "stream"
        self.status = f"RBN {mode}: " + " / ".join(receivers)
        self.diagnostic_status = (f"interval={self.poll_interval:g}s rows={limit}→{stream.load.limit} "
                                  f"avg={stream.load.average} peaks={list(stream.load.peaks)}")
        selected = set(calls)
        return [s for s in stream.spots.values() if s.call in selected or self.friend_index.find(s.call) is not None]
