import os
import random
import csv
from dataclasses import dataclass
from typing import List


@dataclass
class TitleRow:
    """Title text for overlay (3 lines)"""
    line1: str = ""
    line2: str = ""
    line3: str = ""


def load_title_rows(product_dir: str) -> List[TitleRow]:
    """
    Load title text from title.csv in product directory.

    Returns list of TitleRow (usually just 1 row, but can have multiple for rotation)
    Supports encodings: utf-8-sig (Excel), cp874 (Thai), utf-8, tis-620
    """
    if not product_dir or not os.path.isdir(product_dir):
        return []

    csv_path = os.path.join(product_dir, "title.csv")
    if not os.path.exists(csv_path):
        return []

    rows: List[TitleRow] = []
    encodings = ["utf-8-sig", "cp874", "utf-8", "tis-620"]
    content = None

    for enc in encodings:
        try:
            with open(csv_path, "r", encoding=enc, errors="replace") as f:
                content = list(csv.reader(f))
            break
        except Exception:
            continue

    if not content:
        return []

    for r in content:
        if not r:
            continue
        if all(not c.strip() for c in r):
            continue
        if r[0].strip().startswith("#"):
            continue

        c1 = r[0].strip() if len(r) > 0 else ""
        c2 = r[1].strip() if len(r) > 1 else ""
        c3 = r[2].strip() if len(r) > 2 else ""
        rows.append(TitleRow(line1=c1, line2=c2, line3=c3))

    return rows


def select_title_row(rows: List[TitleRow], idx: int, mode: str = "single") -> TitleRow:
    """Select title row based on mode: single/random/round_robin"""
    if not rows:
        return TitleRow()

    count = len(rows)
    if count == 1:
        return rows[0]

    if mode == "random":
        return random.choice(rows)
    if mode == "round_robin":
        safe_idx = max(1, idx)
        return rows[(safe_idx - 1) % count]

    return rows[0]
