"""One-time recalibration analysis for the five_hour quota window.

Phase-0 showed the ``five_hour`` ``tokens_per_pct`` ratio is noisy
(best variant ``with_cc`` at CV 22 %, vs. ``seven_day`` ``io_only`` at
6.9 %). The Phase-0 plan's fallback is to split ``cache_creation`` into
its ephemeral 1h/5m tiers (which claude-monitor's ``UsageEntry`` does
NOT expose) by parsing the raw Claude Code JSONL directly, then test
whether a tier-weighted token model stabilises the 5h ratio.

This script is **read-only** on the raw JSONL under
``~/.claude/projects``. It does NOT touch the live
``logs/quota-calibration.csv`` or ``quota_calibration.py``. It
re-aggregates the historical calibration windows with the 1h/5m split
and prints a CV comparison so we can decide whether the production
schema-v3 change (forward 1h/5m population) is warranted.

Decision gate: does any tier-weighted variant pull the 5h CV below
~15 %? If not, the 5h noise is not a token-weighting problem (more
likely JSONL-flush vs. cclimits-sampling timing drift) and the split
buys nothing.

Run: ``python quota_calibration_backfill.py``
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics as st
from pathlib import Path

from quota_calibration import CSV_FIELDS

# Below this utilization the ratio is dominated by cclimits string-rounding
# noise — mirror quota_calibration._MIN_PCT_FOR_CALIBRATION.
_MIN_PCT_FOR_CALIBRATION = 0.5

CALIB_CSV = Path(__file__).parent / "logs" / "quota-calibration.csv"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


# ───────────────────────────── Raw JSONL loading ─────────────────────────────


class RawEntry:
    """One deduped assistant-message usage record from the raw JSONL."""

    __slots__ = (
        "timestamp", "model", "input", "output",
        "cache_creation", "cache_read", "cc_1h", "cc_5m",
    )

    def __init__(self, timestamp, model, inp, out, cc, cr, cc_1h, cc_5m):
        self.timestamp = timestamp
        self.model = model
        self.input = inp
        self.output = out
        self.cache_creation = cc
        self.cache_read = cr
        self.cc_1h = cc_1h
        self.cc_5m = cc_5m


def _parse_ts(raw: str) -> "dt.datetime | None":
    """Parse an ISO-8601 timestamp; treat naive as UTC, normalise to UTC."""
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def load_raw_entries() -> "list[RawEntry]":
    """Load + dedup all assistant usage records from every project JSONL.

    Dedup key is ``(message.id, requestId)`` — the same key claude-monitor
    uses — so the recomputed totals stay comparable to the existing CSV
    columns (which were produced via claude-monitor).
    """
    seen: set = set()
    entries: list[RawEntry] = []
    files = list(PROJECTS_DIR.rglob("*.jsonl"))
    print(f"scanning {len(files)} JSONL files under {PROJECTS_DIR} ...")
    for fp in files:
        try:
            fh = fp.open(encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                key = (msg.get("id"), obj.get("requestId"))
                if key in seen:
                    continue
                seen.add(key)
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts is None:
                    continue
                cc_block = usage.get("cache_creation") or {}
                entries.append(RawEntry(
                    timestamp=ts,
                    model=msg.get("model", ""),
                    inp=int(usage.get("input_tokens", 0) or 0),
                    out=int(usage.get("output_tokens", 0) or 0),
                    cc=int(usage.get("cache_creation_input_tokens", 0) or 0),
                    cr=int(usage.get("cache_read_input_tokens", 0) or 0),
                    cc_1h=int(cc_block.get("ephemeral_1h_input_tokens", 0) or 0),
                    cc_5m=int(cc_block.get("ephemeral_5m_input_tokens", 0) or 0),
                ))
    entries.sort(key=lambda e: e.timestamp)
    print(f"  -> {len(entries)} deduped usage records "
          f"({entries[0].timestamp.date()} .. {entries[-1].timestamp.date()})")
    return entries


# ───────────────────────────── Window re-aggregation ─────────────────────────


def aggregate_window(entries, window_start, sample_time) -> dict:
    """Sum token counts for entries within [window_start, sample_time].

    The upper bound (sample_time) reproduces the live hook's implicit cap:
    at sample time, entries logged after that instant did not yet exist.
    """
    agg = {"input": 0, "output": 0, "cc": 0, "cr": 0, "cc_1h": 0, "cc_5m": 0, "n": 0}
    for e in entries:
        if e.timestamp < window_start or e.timestamp > sample_time:
            continue
        agg["input"] += e.input
        agg["output"] += e.output
        agg["cc"] += e.cache_creation
        agg["cr"] += e.cache_read
        agg["cc_1h"] += e.cc_1h
        agg["cc_5m"] += e.cc_5m
        agg["n"] += 1
    return agg


def load_calibration_rows() -> list:
    """Load the v2 (26-column) calibration rows, parsed with the correct
    schema (the on-disk header is stale v1, so csv.DictReader misaligns)."""
    raw = list(csv.reader(CALIB_CSV.open(encoding="utf-8")))
    rows = [dict(zip(CSV_FIELDS, r)) for r in raw[1:] if len(r) == 26]
    usable = [
        r for r in rows
        if r["flag_low_pct"] == "false"
        and r["flag_cm_unavailable"] == "false"
        and r["flag_rolling_fallback"] == "false"
    ]
    return usable


# ───────────────────────────── Analysis ──────────────────────────────────────


def _cv(values: list) -> float:
    """Coefficient of variation (population stdev / mean)."""
    m = st.mean(values)
    return st.pstdev(values) / m if m else float("nan")


def recompute(entries, rows) -> list:
    """Attach a recomputed aggregation (with 1h/5m split) to each row."""
    out = []
    for r in rows:
        ws = dt.datetime.fromisoformat(r["window_start_utc"])
        st_ = dt.datetime.fromisoformat(r["timestamp_utc"])
        agg = aggregate_window(entries, ws, st_)
        pct = float(r["cclimits_pct_used"])
        out.append({"row": r, "agg": agg, "pct": pct, "window": r["window"]})
    return out


def sanity_check(recomputed) -> None:
    """Compare recomputed totals against the CSV columns produced by
    claude-monitor. Large divergence would invalidate the 1h/5m split."""
    print("\n=== Sanity check: recomputed vs. CSV (median |rel error|) ===")
    for field, agg_key in (("tokens_input", "input"),
                           ("tokens_output", "output"),
                           ("tokens_cache_creation", "cc"),
                           ("tokens_cache_read", "cr")):
        errs = []
        for item in recomputed:
            csv_val = float(item["row"][field] or 0)
            rec_val = item["agg"][agg_key]
            if csv_val > 0:
                errs.append(abs(rec_val - csv_val) / csv_val)
        if errs:
            print(f"  {field:<26} median {st.median(errs):6.2%}  "
                  f"mean {st.mean(errs):6.2%}  (n={len(errs)})")
    # Verify the tier split actually sums to the total.
    mism = sum(1 for it in recomputed
               if it["agg"]["cc_1h"] + it["agg"]["cc_5m"] != it["agg"]["cc"])
    print(f"  rows where cc_1h + cc_5m != cc_total: {mism}/{len(recomputed)}")


def _variants(agg: dict) -> dict:
    io = agg["input"] + agg["output"]
    return {
        "io_only":     io,
        "with_cc":     io + agg["cc"],
        "cc_1h_only":  io + agg["cc_1h"],
        "cc_5m_only":  io + agg["cc_5m"],
        "all":         io + agg["cc"] + agg["cr"],
    }


def cv_table(recomputed) -> None:
    print("\n=== CV per variant (recomputed, ratio = tokens / pct_used) ===")
    print(f'{"window":<11}{"variant":<13}{"n":>5}{"median tok/%":>16}{"CV":>9}')
    for w in ("five_hour", "seven_day"):
        sub = [it for it in recomputed if it["window"] == w]
        variant_names = list(_variants(sub[0]["agg"]).keys())
        results = []
        for name in variant_names:
            ratios = [_variants(it["agg"])[name] / it["pct"] for it in sub]
            results.append((name, _cv(ratios), st.median(ratios)))
        for name, cv, med in results:
            print(f'{w:<11}{name:<13}{len(sub):>5}{med:>16,.0f}{cv:>8.1%}')
        best = min(results, key=lambda x: x[1])
        print(f'   -> best fixed-variant: {best[0]} (CV {best[1]:.1%})\n')


def grid_search_5h(recomputed) -> None:
    """Search (w_1h, w_5m) for tokens = io + w1*cc_1h + w5*cc_5m that
    minimises the five_hour ratio CV. Brackets whether tier reweighting
    can beat the equal-weight ``with_cc`` baseline."""
    sub = [it for it in recomputed if it["window"] == "five_hour"]
    weights = [i / 20 for i in range(0, 41)]  # 0.00 .. 2.00 step 0.05
    best = (float("inf"), None, None)
    for w1 in weights:
        for w5 in weights:
            ratios = []
            for it in sub:
                a = it["agg"]
                tok = a["input"] + a["output"] + w1 * a["cc_1h"] + w5 * a["cc_5m"]
                ratios.append(tok / it["pct"])
            cv = _cv(ratios)
            if cv < best[0]:
                best = (cv, w1, w5)
    print("=== 5h grid search: io + w1*cc_1h + w5*cc_5m ===")
    print(f"  best CV {best[0]:.1%} at w_1h={best[1]:.2f}, w_5m={best[2]:.2f}")
    print(f"  (baseline with_cc = w1=w5=1.0; decision gate = CV < 15%)")
    print(f"  VERDICT: {'tier reweighting HELPS' if best[0] < 0.15 else 'tier reweighting does NOT reach <15% — 5h noise is not a weighting problem'}")


def main() -> None:
    entries = load_raw_entries()
    rows = load_calibration_rows()
    print(f"usable v2 calibration rows: {len(rows)}")
    recomputed = recompute(entries, rows)
    sanity_check(recomputed)
    cv_table(recomputed)
    print()
    grid_search_5h(recomputed)


if __name__ == "__main__":
    main()
