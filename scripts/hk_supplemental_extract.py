#!/usr/bin/env python3
"""
Supplemental HK extraction with looser quote-side filters.

This keeps the original focused screen intact and writes separate loose-batch
outputs. It reuses the same Tushare financial cache and metric model.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

from hk_netcash_screen import (
    BAD_NAME_PARTS,
    OUT_DIR,
    compute_metrics,
    pull_financials_for_code,
)


def load_cached_financials(cache_path: Path) -> dict[str, pd.DataFrame]:
    cache_obj = json.loads(cache_path.read_text())
    fin = {}
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


def quote_filters(universe: pd.DataFrame) -> pd.DataFrame:
    df = universe.copy()
    for col in ["total_mv_hkd", "turnover_hkd", "pe_ttm", "pe_dynamic", "pb", "price_hkd"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    name = df["name"].fillna(df["em_name"]).fillna("")
    bad = name.str.upper().apply(lambda x: any(part.upper() in x for part in BAD_NAME_PARTS))
    pe = df["pe_ttm"].where(df["pe_ttm"].notna(), df["pe_dynamic"])
    df["prefilter_pe"] = pe

    # Original 管我财-style quote gate.
    old_mask = (
        df["price_hkd"].gt(0)
        & df["total_mv_hkd"].between(2e8, 8e10)
        & df["pb"].between(0.05, 1.8)
        & ((pe.between(0.5, 25)) | pe.isna())
        & df["turnover_hkd"].fillna(0).between(5e4, 1.5e8)
        & ~bad
    )

    # Looser gate: still excludes funds/derivatives, but allows larger quality
    # names, slightly higher PB/PE, and very thin-but-tradable names.
    loose_mask = (
        df["price_hkd"].gt(0)
        & df["total_mv_hkd"].between(1e8, 3e11)
        & df["pb"].between(0.05, 3.5)
        & ((pe.between(0.1, 45)) | pe.isna())
        & df["turnover_hkd"].fillna(0).between(1e4, 5e8)
        & ~bad
    )

    out = df.loc[loose_mask].copy()
    out["source_batch"] = np.where(old_mask.loc[out.index], "原始初筛", "补充宽筛")
    out["prefilter_rank"] = (
        out["pb"].rank(pct=True, ascending=True).fillna(0.7) * 0.28
        + out["total_mv_hkd"].rank(pct=True, ascending=True).fillna(0.7) * 0.22
        + out["turnover_hkd"].rank(pct=True, ascending=True).fillna(0.7) * 0.18
        + out["prefilter_pe"].rank(pct=True, ascending=True).fillna(0.7) * 0.16
        + out["source_batch"].eq("原始初筛").astype(int) * 0.16
    )
    return out.sort_values("prefilter_rank")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    universe = pd.read_csv(OUT_DIR / "hk_merged_universe.csv").drop_duplicates("ts_code")
    selected = quote_filters(universe).drop_duplicates("ts_code")
    selected.to_csv(OUT_DIR / "hk_loose_prefilter.csv", index=False)

    cache_dir = OUT_DIR / "financial_cache"
    cache_dir.mkdir(exist_ok=True)
    pro = ts.pro_api()
    rows = []
    pulled = 0

    print(f"Loose quote candidates: {len(selected)}")
    for idx, row in selected.reset_index(drop=True).iterrows():
        code = row["ts_code"]
        cache_path = cache_dir / f"{code.replace('.', '_')}.json"
        if cache_path.exists():
            fin = load_cached_financials(cache_path)
            status = "cache"
        else:
            print(f"[pull {pulled + 1}] {idx + 1}/{len(selected)} {code} {row.get('name') or row.get('em_name')}")
            fin = pull_financials_for_code(pro, code)
            save_cached_financials(cache_path, fin)
            pulled += 1
            status = "new"
            time.sleep(0.04)

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
            "source_batch",
        ]:
            m[key] = row.get(key)
        m["financial_source"] = status
        rows.append(m)

    metrics = pd.DataFrame(rows)
    if metrics.empty:
        raise SystemExit("No supplemental metrics produced")

    financial_gate = (
        (metrics["cash_like"] > metrics["interest_debt"].fillna(0))
        & (metrics["profit_latest"] > 0)
        & (metrics["profit_positive_years"] >= 2)
        & (metrics["latest_report_date"].astype(str) >= "20241231")
    )
    ranked = metrics.loc[financial_gate].copy()
    ranked["strict_net_cash"] = ranked["cash_like"] > ranked["total_liabilities"]
    ranked = ranked.sort_values(["strict_net_cash", "score"], ascending=[False, False])

    ranked.to_csv(OUT_DIR / "hk_loose_ranked_candidates.csv", index=False)
    metrics.sort_values("score", ascending=False).to_csv(OUT_DIR / "hk_loose_all_scored.csv", index=False)
    ranked.loc[ranked["source_batch"].eq("补充宽筛")].to_csv(OUT_DIR / "hk_supplemental_candidates.csv", index=False)

    print(f"Pulled new financials: {pulled}")
    print(f"Loose financial pass: {len(ranked)}")
    print(f"Supplemental pass: {int(ranked['source_batch'].eq('补充宽筛').sum())}")
    print(OUT_DIR / "hk_loose_ranked_candidates.csv")
    print(OUT_DIR / "hk_supplemental_candidates.csv")


if __name__ == "__main__":
    main()
