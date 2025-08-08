#!/usr/bin/env python3
import argparse
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

try:
    import openpyxl  # noqa: F401
    HAS_OPENPYXL = True
except Exception:
    HAS_OPENPYXL = False


CANONICAL_COLUMNS = [
    "time",
    "type",
    "details",
    "amount",
    "asset",
    "symbol",
    "note",
]


def detect_format(file_path: Path) -> str:
    with open(file_path, "rb") as f:
        head = f.read(4)
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    return "csv"


def try_read_csv(file_path: Path) -> pd.DataFrame:
    # Try utf-8 first, then cp1252 fallback
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin1"):
        try:
            return pd.read_csv(file_path, encoding=enc)
        except UnicodeDecodeError:
            continue
    # Last resort without encoding hint
    return pd.read_csv(file_path)


def read_any(file_path: Path) -> pd.DataFrame:
    fmt = detect_format(file_path)
    if fmt == "xlsx":
        if not HAS_OPENPYXL:
            raise RuntimeError(
                "openpyxl is required to read .xlsx files. Install with: pip install openpyxl"
            )
        df = pd.read_excel(file_path, sheet_name=0, dtype=str, engine="openpyxl")
    else:
        df = try_read_csv(file_path)
    return df


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    original_cols = list(df.columns)
    lower_map: Dict[str, str] = {str(c).strip().lower(): c for c in original_cols}

    # Known variations mapping -> canonical
    mapping: Dict[str, str] = {
        "time(utc+8)": "time",
        "time": "time",
        "type": "type",
        "details": "details",
        "amount": "amount",
        "newavailableamount": "note",  # account balance after op – keep as note
        "assets": "asset",
        "asset": "asset",
        "futures": "symbol",
        "symbol": "symbol",
        "unnamed: 7": "note",
        "note": "note",
        "comment": "note",
        "remarks": "note",
    }

    out: Dict[str, pd.Series] = {}
    for low, orig in lower_map.items():
        canon = mapping.get(low)
        if canon:
            out[canon] = df[orig]

    # Ensure all canonical columns present
    for col in CANONICAL_COLUMNS:
        if col not in out:
            out[col] = pd.Series([None] * len(df))

    nd = pd.DataFrame(out)

    # Coerce amount to numeric (float), preserve original if needed
    nd["amount"] = pd.to_numeric(nd["amount"], errors="coerce")

    # Strip whitespace
    for c in ["time", "type", "details", "asset", "symbol", "note"]:
        nd[c] = nd[c].astype(str).str.strip()

    # Normalize newlines in note/details
    for c in ["details", "note"]:
        nd[c] = nd[c].astype(str).str.replace("\r\n", "\n").str.replace("\r", "\n")

    # Try parse datetime (keep original text if parse fails)
    try:
        nd["time_parsed"] = pd.to_datetime(nd["time"], errors="coerce")
    except Exception:
        nd["time_parsed"] = pd.NaT

    return nd


@dataclass
class Summary:
    start_time: Optional[pd.Timestamp]
    end_time: Optional[pd.Timestamp]
    total_rows: int
    trading_fee_sum: float
    trading_fee_abs_sum: float
    funding_fee_sum: float
    funding_fee_abs_sum: float
    realized_pnl_sum: float
    turnover_estimate: Optional[float]
    net_profit_usdt: float
    net_profit_pct_of_turnover: Optional[float]


def compute_summary(df: pd.DataFrame, fee_rate: Optional[float]) -> Summary:
    start = None
    end = None
    if "time_parsed" in df.columns:
        non_na = df["time_parsed"].dropna()
        if not non_na.empty:
            start = non_na.min()
            end = non_na.max()

    def sum_where(cond: pd.Series) -> Tuple[float, float]:
        s = df.loc[cond, "amount"].fillna(0.0)
        return float(s.sum()), float(s.abs().sum())

    trading_fee_sum, trading_fee_abs_sum = sum_where(df["type"].str.lower() == "trading fee")
    funding_fee_sum, funding_fee_abs_sum = sum_where(df["type"].str.lower() == "funding fee")
    realized_pnl_sum, _ = sum_where(df["type"].str.lower() == "realized pnl")

    turnover_estimate = None
    if fee_rate and fee_rate > 0:
        # For futures, total volume across open+close sides approximates abs(fees)/fee_rate
        turnover_estimate = trading_fee_abs_sum / fee_rate

    # Net profit assumes realized PnL minus absolute fees (trading + funding)
    net_profit_usdt = realized_pnl_sum - (trading_fee_abs_sum + funding_fee_abs_sum)
    net_profit_pct = None
    if turnover_estimate and turnover_estimate > 0:
        net_profit_pct = (net_profit_usdt / turnover_estimate) * 100.0

    return Summary(
        start_time=start,
        end_time=end,
        total_rows=len(df),
        trading_fee_sum=trading_fee_sum,
        trading_fee_abs_sum=trading_fee_abs_sum,
        funding_fee_sum=funding_fee_sum,
        funding_fee_abs_sum=funding_fee_abs_sum,
        realized_pnl_sum=realized_pnl_sum,
        turnover_estimate=turnover_estimate,
        net_profit_usdt=net_profit_usdt,
        net_profit_pct_of_turnover=net_profit_pct,
    )


def write_report(report_path: Path, summary: Summary, df: pd.DataFrame, fee_rate: Optional[float]) -> None:
    by_symbol = (
        df.groupby(df["symbol"].fillna("").astype(str).str.strip())
        .agg(
            rows=("amount", "count"),
            trading_fee_sum=("amount", lambda s: s[df.loc[s.index, "type"].str.lower() == "trading fee"].sum()),
            trading_fee_abs_sum=(
                "amount",
                lambda s: s[df.loc[s.index, "type"].str.lower() == "trading fee"].abs().sum(),
            ),
            funding_fee_sum=("amount", lambda s: s[df.loc[s.index, "type"].str.lower() == "funding fee"].sum()),
            realized_pnl_sum=("amount", lambda s: s[df.loc[s.index, "type"].str.lower() == "realized pnl"].sum()),
        )
        .sort_values(by=["trading_fee_abs_sum"], ascending=False)
    )

    lines: List[str] = []
    lines.append("Report: Futures turnover and fees")
    if summary.start_time and summary.end_time:
        lines.append(f"Period: {summary.start_time} -> {summary.end_time}")
    lines.append(f"Rows: {summary.total_rows}")
    lines.append("")
    lines.append("Totals:")
    lines.append(f"  Trading Fee sum (signed): {summary.trading_fee_sum:.8f}")
    lines.append(f"  Trading Fee sum (abs):    {summary.trading_fee_abs_sum:.8f}")
    lines.append(f"  Funding Fee sum (signed): {summary.funding_fee_sum:.8f}")
    lines.append(f"  Funding Fee sum (abs):    {summary.funding_fee_abs_sum:.8f}")
    lines.append(f"  Realized PnL sum:         {summary.realized_pnl_sum:.8f}")
    if summary.turnover_estimate is not None:
        lines.append(f"  Turnover estimate (@fee_rate={fee_rate}): {summary.turnover_estimate:.8f}")
    else:
        lines.append("  Turnover estimate: (fee_rate not provided; pass --fee-rate to compute)")
    lines.append(f"  Net Profit (USDT):        {summary.net_profit_usdt:.8f}")
    if summary.net_profit_pct_of_turnover is not None:
        lines.append(f"  Net Profit (% of turnover): {summary.net_profit_pct_of_turnover:.6f}%")
    else:
        lines.append("  Net Profit (%): (requires --fee-rate to compute turnover)")
    lines.append("")
    lines.append("By symbol:")
    lines.append("symbol,rows,trading_fee_sum,trading_fee_abs_sum,funding_fee_sum,realized_pnl_sum")
    for rec in by_symbol.reset_index().itertuples(index=False):
        sym = getattr(rec, "symbol")
        rows_val = float(getattr(rec, "rows"))
        t_sum = float(getattr(rec, "trading_fee_sum"))
        t_abs = float(getattr(rec, "trading_fee_abs_sum"))
        f_sum = float(getattr(rec, "funding_fee_sum"))
        r_sum = float(getattr(rec, "realized_pnl_sum"))
        lines.append(
            f"{sym},{int(rows_val)},{t_sum:.8f},{t_abs:.8f},{f_sum:.8f},{r_sum:.8f}"
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def save_normalized_csv(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert exchange export to normalized CSV and compute fees/turnover")
    ap.add_argument("input", type=Path, help="Path to input file (.csv or .xlsx; misnamed .csv with PK header is auto-detected)")
    ap.add_argument("--normalized-csv", type=Path, default=None, help="Path to write normalized CSV (UTF-8)")
    ap.add_argument("--report", type=Path, default=None, help="Path to write text report")
    ap.add_argument("--fee-rate", type=float, default=None, help="Trading fee rate (e.g., 0.0006). Used to estimate turnover from fees.")

    args = ap.parse_args()

    df_raw = read_any(args.input)
    df_norm = normalize_columns(df_raw)

    # If no explicit normalized path, place alongside input
    if args.normalized_csv is None:
        default_norm = args.input.with_suffix("")
        args.normalized_csv = default_norm.parent / f"{default_norm.name}.normalized.csv"
    save_normalized_csv(df_norm, args.normalized_csv)

    summary = compute_summary(df_norm, args.fee_rate)

    # If no explicit report path, place alongside input
    if args.report is None:
        default_rep = args.input.with_suffix("")
        args.report = default_rep.parent / f"{default_rep.name}.report.txt"

    write_report(args.report, summary, df_norm, args.fee_rate)

    print(f"Normalized CSV written to: {args.normalized_csv}")
    print(f"Report written to:        {args.report}")


if __name__ == "__main__":
    main()


