"""Generate the frozen second-judge measurement sample.

The 08-11/12 judge measurements used two hand-picked slices (the deterministic
first-25, which is ~all truncated Adzuna, and a LinkedIn-only full-text set).
Neither matches the population a daily second judge would actually score:
status='evaluated' fit>=15 in the last 30 days is ~57% Adzuna snippets and
~43% LinkedIn full text. This script freezes a stratified random sample of
that population so every judge candidate is measured on the same, honest mix.

Frozen means frozen: the sample is a list of job_urls in
judge_sample.local.json (gitignored — real postings). Re-running this script
refuses to overwrite unless --regenerate is passed; judge comparisons across
different sample generations are not comparable.

Usage:
    python tests/validation/make_judge_sample.py [--regenerate]
"""
import datetime as dt
import json
import pathlib
import random
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _common import DB_PATH  # noqa: E402

OUT_PATH = pathlib.Path(__file__).with_name("judge_sample.local.json")
SAMPLE_N = 50
FIT_BAR = 15
WINDOW_DAYS = 30
SEED = 20260812
# Dice/ATS round to zero seats at their true share; keep one Dice row anyway
# as a witness for the only other snippet-flavor source the pipeline has.
FORCED = {"dice": 1}


def main():
    if OUT_PATH.exists() and "--regenerate" not in sys.argv[1:]:
        sys.exit(f"{OUT_PATH.name} already exists — frozen means frozen. "
                 f"Pass --regenerate if you really want a new, incomparable sample.")

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT job_url, source FROM jobs "
        "WHERE status='evaluated' AND fit_score >= ? "
        "AND first_seen >= date('now', ?) AND length(trim(description)) > 0 "
        "ORDER BY job_url", (FIT_BAR, f"-{WINDOW_DAYS} day")
    ).fetchall()
    con.close()

    by_src = {}
    for r in rows:
        by_src.setdefault(r["source"], []).append(r["job_url"])
    total = len(rows)
    if total < SAMPLE_N:
        sys.exit(f"population too small ({total} < {SAMPLE_N})")

    # Proportional seats, forced minima, remainder adjusted on the largest stratum.
    seats = {src: round(SAMPLE_N * len(urls) / total) for src, urls in by_src.items()}
    for src, k in FORCED.items():
        if src in by_src:
            seats[src] = max(seats.get(src, 0), k)
    largest = max(seats, key=lambda s: len(by_src[s]))
    seats[largest] += SAMPLE_N - sum(seats.values())

    rng = random.Random(SEED)
    picked = {src: sorted(rng.sample(by_src[src], k))
              for src, k in seats.items() if k > 0}

    payload = {
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "seed": SEED,
        "population": {"status": "evaluated", "fit_bar": FIT_BAR,
                       "window_days": WINDOW_DAYS, "total": total,
                       "by_source": {s: len(u) for s, u in by_src.items()}},
        "seats": seats,
        "job_urls": sorted(u for urls in picked.values() for u in urls),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"froze {sum(seats.values())} rows -> {OUT_PATH.name}")
    print(f"population n={total}: " + ", ".join(
        f"{s}={len(u)} ({100 * len(u) / total:.0f}%)" for s, u in sorted(by_src.items())))
    print("seats: " + ", ".join(f"{s}={k}" for s, k in sorted(seats.items()) if k))


if __name__ == "__main__":
    main()
