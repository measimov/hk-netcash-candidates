#!/usr/bin/env python3
"""Refresh HK candidate rankings with Tencent latest HK quotes.

This script leaves the Tushare financial cache intact and only refreshes the
market-price side: price, turnover, market cap, PE, and PB.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from hk_netcash_screen import OUT_DIR, build_prefilter, compute_metrics, pull_financials_for_code


TENCENT_URL = "https://qt.gtimg.cn/q={symbols}"
UA = {"User-Agent": "Mozilla/5.0"}


def to_num(value):
    return pd.to_numeric(value, errors="coerce")


def parse_quote_line(body: str) -> dict | None:
    fields = body.split("~")
    if len(fields) < 46:
        return None
    symbol = str(fields[2]).zfill(5)
    price = to_num(fields[3])
    if pd.isna(price) or float(price) <= 0:
        return None
    return {
        "symbol": symbol,
        "ts_code": f"{symbol}.HK",
        "tencent_name": fields[1],
        "price_hkd": float(price),
        "prev_close": to_num(fields[4]),
        "latest_open": to_num(fields[5]),
        "volume_shares": to_num(fields[36] if len(fields) > 36 else np.nan),
        "turnover_hkd": to_num(fields[37] if len(fields) > 37 else np.nan),
        "quote_time": fields[30] if len(fields) > 30 else "",
        "pct_chg": to_num(fields[32] if len(fields) > 32 else np.nan),
        "latest_high": to_num(fields[33] if len(fields) > 33 else np.nan),
        "latest_low": to_num(fields[34] if len(fields) > 34 else np.nan),
        # Tencent HK market cap fields are in HKD 100m.
        "total_mv_hkd": to_num(fields[44] if len(fields) > 44 else np.nan) * 1e8,
        "float_mv_hkd": to_num(fields[45] if len(fields) > 45 else np.nan) * 1e8,
        "tencent_pe": to_num(fields[39] if len(fields) > 39 else np.nan),
        "tencent_pb": to_num(fields[43] if len(fields) > 43 else np.nan),
    }


def fetch_tencent_quotes(codes: list[str], batch_size: int = 80, pause: float = 0.12) -> pd.DataFrame:
    rows: list[dict] = []
    session = requests.Session()
    session.headers.update(UA)
    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        symbols = ",".join(f"hk{str(code).split('.')[0].zfill(5)}" for code in batch)
        resp = session.get(TENCENT_URL.format(symbols=symbols), timeout=20)
        resp.raise_for_status()
        for part in resp.text.split(";\n"):
            if '="' not in part:
                continue
            body = part.split('="', 1)[1].rstrip('";')
            parsed = parse_quote_line(body)
            if parsed:
                rows.append(parsed)
        time.sleep(pause)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates("ts_code", keep="last")


def load_cached_financials(cache_path: Path) -> dict[str, pd.DataFrame]:
    cache_obj = json.loads(cache_path.read_text())
    fin: dict[str, pd.DataFrame] = {}
    for ep in ("hk_balancesheet", "hk_income", "hk_cashflow"):
        payload = cache_obj.get(ep, {})
        df = pd.DataFrame(payload.get("data", []))
        if not df.empty:
            df = df.set_index("end_date")
            df.columns.name = "ind_name"
        fin[ep] = df
    return fin


def save_cached_financials(cache_path: Path, fin: dict[str, pd.DataFrame]) -> None:
    cache_obj = {}
    for ep in ("hk_balancesheet", "hk_income", "hk_cashflow"):
        df = fin.get(ep, pd.DataFrame())
        cache_obj[ep] = {"data": df.reset_index().to_dict(orient="records") if not df.empty else []}
    cache_path.write_text(json.dumps(cache_obj, ensure_ascii=False))


def merge_quotes_into_universe(universe: pd.DataFrame, quotes: pd.DataFrame) -> pd.DataFrame:
    out = universe.copy()
    original_cols = set(out.columns)
    old_price = pd.to_numeric(out.get("price_hkd"), errors="coerce")
    old_total_mv = pd.to_numeric(out.get("total_mv_hkd"), errors="coerce")
    old_float_mv = pd.to_numeric(out.get("float_mv_hkd"), errors="coerce")
    qcols = [
        "ts_code",
        "price_hkd",
        "pct_chg",
        "volume_shares",
        "turnover_hkd",
        "latest_high",
        "latest_low",
        "latest_open",
        "prev_close",
        "quote_time",
    ]
    merged = out.merge(quotes[qcols], on="ts_code", how="left", suffixes=("", "_tencent"))
    for col in [c for c in qcols if c != "ts_code"]:
        tcol = f"{col}_tencent" if col in original_cols else col
        if tcol not in merged.columns:
            continue
        if col in original_cols:
            merged[col] = merged[tcol].where(merged[tcol].notna(), merged[col])
            merged.drop(columns=[tcol], inplace=True)
    price_ratio = pd.to_numeric(merged["price_hkd"], errors="coerce") / old_price.replace(0, np.nan)
    for col in ["pb", "pe_ttm", "pe_dynamic"]:
        if col in merged.columns:
            original = pd.to_numeric(out.get(col), errors="coerce")
            adjusted = original * price_ratio
            merged[col] = adjusted.where(adjusted.notna(), merged[col])
    if "total_mv_hkd" in merged.columns:
        adjusted_total = old_total_mv * price_ratio
        merged["total_mv_hkd"] = adjusted_total.where(adjusted_total.notna(), merged["total_mv_hkd"])
    if "float_mv_hkd" in merged.columns:
        adjusted_float = old_float_mv * price_ratio
        merged["float_mv_hkd"] = adjusted_float.where(adjusted_float.notna(), merged["float_mv_hkd"])
    merged["quote_source"] = np.where(merged["quote_time"].notna() & merged["quote_time"].astype(str).ne(""), "tencent", "fallback_cache")
    return merged


def recompute_primary(universe: pd.DataFrame, max_codes: int, pull_missing: bool) -> pd.DataFrame:
    pre = build_prefilter(universe)
    pre.to_csv(OUT_DIR / "hk_prefilter.csv", index=False)
    selected = pre.drop_duplicates("ts_code").head(max_codes).copy()
    cache_dir = OUT_DIR / "financial_cache"
    cache_dir.mkdir(exist_ok=True)
    rows: list[dict] = []

    pro = None
    for _, row in selected.reset_index(drop=True).iterrows():
        code = row["ts_code"]
        cache_path = cache_dir / f"{code.replace('.', '_')}.json"
        if cache_path.exists():
            fin = load_cached_financials(cache_path)
            status = "cache"
        elif pull_missing:
            if pro is None:
                import tushare as ts

                pro = ts.pro_api()
            fin = pull_financials_for_code(pro, code)
            save_cached_financials(cache_path, fin)
            status = "new"
        else:
            continue
        metric = compute_metrics(code, fin, row["total_mv_hkd"], row["price_hkd"])
        if metric is None:
            continue
        m = metric.__dict__
        for key in [
            "name",
            "fullname",
            "market",
            "list_date",
            "price_hkd",
            "total_mv_hkd",
            "float_mv_hkd",
            "turnover_hkd",
            "pb",
            "pe_ttm",
            "pe_dynamic",
            "ts_avg_amount_hkd",
            "ts_median_amount_hkd",
            "quote_time",
            "quote_source",
        ]:
            m[key] = row.get(key)
        m["financial_source"] = status
        rows.append(m)

    metrics = pd.DataFrame(rows)
    if metrics.empty:
        raise SystemExit("No metrics produced")

    financial_gate = (
        (metrics["cash_like"] > metrics["interest_debt"].fillna(0))
        & (metrics["profit_latest"] > 0)
        & (metrics["profit_positive_years"] >= 2)
        & (metrics["latest_report_date"].astype(str) >= "20241231")
    )
    ranked = metrics.loc[financial_gate].copy()
    ranked["strict_net_cash"] = ranked["cash_like"] > ranked["total_liabilities"]
    ranked["liquidity_bucket"] = pd.cut(
        ranked["turnover_hkd"],
        bins=[-math.inf, 2e5, 1e6, 1e7, 5e7, math.inf],
        labels=["极低", "低", "中低", "中", "高"],
    )
    ranked = ranked.sort_values(["strict_net_cash", "score"], ascending=[False, False])
    ranked.to_csv(OUT_DIR / "hk_ranked_candidates.csv", index=False)
    metrics.sort_values("score", ascending=False).to_csv(OUT_DIR / "hk_all_scored_prefilter.csv", index=False)
    return ranked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-codes", type=int, default=420)
    parser.add_argument("--pull-missing", action="store_true")
    args = parser.parse_args()

    universe_path = OUT_DIR / "hk_merged_universe.csv"
    universe = pd.read_csv(universe_path).drop_duplicates("ts_code")
    quotes = fetch_tencent_quotes(universe["ts_code"].astype(str).tolist())
    if quotes.empty:
        raise SystemExit("No Tencent quotes fetched")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quotes_path = OUT_DIR / f"tencent_hk_quotes_{stamp}.csv"
    latest_path = OUT_DIR / "tencent_hk_quotes_latest.csv"
    quotes.to_csv(quotes_path, index=False)
    quotes.to_csv(latest_path, index=False)

    refreshed = merge_quotes_into_universe(universe, quotes)
    refreshed.to_csv(universe_path, index=False)
    ranked = recompute_primary(refreshed, args.max_codes, args.pull_missing)

    times = quotes["quote_time"].dropna().astype(str)
    print(f"Tencent quotes: {len(quotes)} / {len(universe)}")
    print(f"Quote time range: {times.min() if len(times) else ''} -> {times.max() if len(times) else ''}")
    print(f"Primary ranked: {len(ranked)}")
    print(quotes_path)
    print(OUT_DIR / "hk_ranked_candidates.csv")


if __name__ == "__main__":
    main()
