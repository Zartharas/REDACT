"""
Extended flattened-username layer (PCCF phase 4, F2b). Additive: it does
NOT modify flattened_names.py. Its output is the existing layer's hits
UNION new ones.

WHY: the phase-3 diagnosis showed that the existing layer
(flattened_names.py) misses whole username SHAPES rather than just
names: given name + digits ("calvin52") and initial + surname
("tadams"). Separately, validation/real_name_frequency measured 15.2%
recall on real SSA/Census <given><surname> tokens because the layer's
dictionary is Faker en_US.

WHAT IT ADDS, all built on REAL name-frequency lists (public-domain SSA
given names 2016-2025 and 2010 Census surnames, already staged under
validation/real_name_frequency/raw/), which are independent of the Faker
data used to inject test names:
  1. <given><surname> / <surname><given>, with or without one [._-]
     separator
  2. <initial><surname>, surname >= 4 letters, token all lowercase
  3. <given><2-4 digits>, token all lowercase
List sizes are fixed in advance: given names with >= 5,000 SSA births
over 2016-2025, and surnames ranked in the Census top 20,000.

If the SSA/Census zips are absent, the new shapes are disabled
(new_shapes_available() returns False) rather than silently falling back
to Faker lists, which would make any evaluation circular.
"""
from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from functools import lru_cache

import flattened_names

_RAW = os.path.join(os.path.dirname(__file__), "..", "validation", "real_name_frequency", "raw")
GIVEN_MIN_COUNT = 5000
SURNAME_TOP_N = 20000
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9._-]{3,29}\b")
_DIGITS_RE = re.compile(r"(\d{1,4})$")


@lru_cache(maxsize=1)
def _lists():
    census, ssa = os.path.join(_RAW, "census_surnames.zip"), os.path.join(_RAW, "ssa_given_names.zip")
    if not (os.path.exists(census) and os.path.exists(ssa)):
        return None, None
    surnames = []
    with zipfile.ZipFile(census) as z, z.open("Names_2010Census.csv") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace")):
            if row["name"] and row["name"] != "ALL OTHER NAMES":
                try:
                    surnames.append((int(row["count"]), row["name"].lower()))
                except ValueError:
                    pass
    surnames.sort(reverse=True)
    sur = {n for _, n in surnames[:SURNAME_TOP_N] if len(n) >= 3 and n.isalpha()}
    given = {}
    with zipfile.ZipFile(ssa) as z:
        for year in range(2016, 2026):
            fn = f"yob{year}.txt"
            if fn not in z.namelist():
                continue
            with z.open(fn) as f:
                for line in io.TextIOWrapper(f, encoding="utf-8", errors="replace"):
                    p = line.strip().split(",")
                    if len(p) == 3:
                        given[p[0].lower()] = given.get(p[0].lower(), 0) + int(p[2])
    giv = {n for n, c in given.items() if c >= GIVEN_MIN_COUNT and len(n) >= 3 and n.isalpha()}
    return giv, sur


def new_shapes_available() -> bool:
    return _lists()[0] is not None


def _match(tok: str):
    giv, sur = _lists()
    low = tok.lower()
    parts = re.split(r"[._-]", low)
    if len(parts) == 2 and all(parts):
        a, b = parts
        if (a in giv and b in sur) or (a in sur and b in giv):
            return "given+surname(sep)"
        if len(a) == 1 and len(b) >= 4 and b in sur:
            return "initial+surname(sep)"
        return None
    if len(parts) != 1:
        return None
    m = _DIGITS_RE.search(low)
    digits = m.group(1) if m else ""
    core = low[: len(low) - len(digits)] if digits else low
    if not core.isalpha():
        return None
    for i in range(3, len(core) - 2):
        a, b = core[:i], core[i:]
        if (a in giv and b in sur) or (a in sur and b in giv):
            return "given+surname"
    if tok.islower() and not digits and len(core) >= 5 and core[1:] in sur:
        return "initial+surname"
    if tok.islower() and 2 <= len(digits) <= 4 and core in giv:
        return "given+digits"
    return None


ALL_SHAPES = ("given+surname", "initial+surname", "given+digits")


def scan_flattened_ext(text: str, shapes=ALL_SHAPES) -> list[dict]:
    """shapes: which new shapes to enable (the separator variants follow
    their base shape). Default: all three, as pre-registered."""
    hits = list(flattened_names.scan_flattened_names(text))
    if not new_shapes_available():
        return hits
    for m in _TOKEN_RE.finditer(text):
        if text[m.end():m.end() + 1] == "@":
            continue
        if any(h["start"] < m.end() and m.start() < h["end"] for h in hits):
            continue
        shape = _match(m.group(0))
        if shape and shape.replace("(sep)", "") in shapes:
            hits.append({"type": "PERSON", "start": m.start(), "end": m.end(),
                         "method": f"flattened_ext:{shape}"})
    return hits
