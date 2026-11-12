"""Application constants and radio-band definitions."""

from __future__ import annotations

import re
from datetime import timezone


POTA_URL = "https://api.pota.app/spot/activator"


WWFF_URL = "https://spots.wwff.co/static/spots.json"


PROGRAMS = ("POTA", "SOTA", "WWFF")


RBN_URL = "https://www.reversebeacon.net"


USER_AGENT = "cw-activation-monitor/0.3.0 (read-only activation/RBN monitor)"
MAX_RESPONSE_BYTES = 8_000_000


UTC = timezone.utc


CALL_PATTERN = re.compile(r"[A-Z0-9]+(?:/[A-Z0-9]+)*(?:-\d+)?")


BANDS = (
    (135.7, 137.8, "2190m"), (472, 479, "630m"), (501, 504, "560m"),
    (1800, 2000, "160m"), (3500, 4000, "80m"), (5060, 5450, "60m"),
    (7000, 7300, "40m"), (10100, 10150, "30m"), (14000, 14350, "20m"),
    (18068, 18168, "17m"), (21000, 21450, "15m"), (24890, 24990, "12m"),
    (28000, 29700, "10m"), (40000, 45000, "8m"), (50000, 54000, "6m"),
    (54000.001, 69900, "5m"), (70000, 71000, "4m"), (144000, 148000, "2m"),
    (222000, 225000, "1.25m"), (420000, 450000, "70cm"),
    (902000, 928000, "33cm"), (1240000, 1300000, "23cm"),
    (2300000, 2450000, "13cm"), (3300000, 3500000, "9cm"),
    (5650000, 5925000, "6cm"), (10000000, 10500000, "3cm"),
    (24000000, 24250000, "1.25cm"), (47000000, 47200000, "6mm"),
    (75500000, 81000000, "4mm"), (119980000, 123000000, "2.5mm"),
    (134000000, 149000000, "2mm"), (241000000, 250000000, "1mm"),
    (275000000, 30000000000, "submm"),
)
