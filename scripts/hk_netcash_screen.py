#!/usr/bin/env python3
"""
Screen Hong Kong stocks for a "net-cash, low valuation, shareholder return"
style candidate list.

Primary financial data source: Tushare Pro HK statement endpoints.
Supplemental market data source: Eastmoney delayed HK quote API.

The script intentionally does not read or persist the Tushare token. It relies on
the local Tushare token configuration used by tushare.pro_api().
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests
import tushare as ts

from hk_markdown import df_to_markdown


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve()


BAD_NAME_PARTS = (
    "ETF",
    "ETN",
    "基金",
    "REIT",
    "牛",
    "熊",
    "认购",
    "认沽",
    "权证",
    "债",
    "票据",
)


BS_ALIASES = {
    "cash": ["现金及等价物", "现金及现金等价物"],
    "short_deposit": ["短期存款", "定期存款", "存款"],
    "short_invest": [
        "短期投资",
        "证券投资",
        "指定以公允价值记账之金融资产(流动)",
        "其他金融资产(流动)",
    ],
    "restricted_cash": ["受限制存款及现金"],
    "total_liabilities": ["总负债"],
    "current_liabilities": ["流动负债合计"],
    "noncurrent_liabilities": ["非流动负债合计"],
    "short_debt": ["短期贷款", "银行贷款及透支", "借款"],
    "long_debt": ["长期贷款", "长期银行贷款", "可转换票据及债券", "应付票据(非流动)"],
    "lease_current": ["融资租赁负债(流动)"],
    "lease_noncurrent": ["融资租赁负债(非流动)"],
    "receivables": ["应收帐款", "应收账款", "预付款按金及其他应收款", "预付款项"],
    "inventory": ["存货"],
    "total_assets": ["总资产"],
    "equity": ["股东权益", "股东权益合计", "归属于母公司股东权益", "净资产"],
}

INCOME_ALIASES = {
    "revenue": ["营业额", "经营收入", "经营收入总额", "营运收入"],
    "gross_profit": ["毛利"],
    "operating_profit": ["经营溢利"],
    "pretax_profit": ["除税前溢利", "除税前溢利(业务利润)"],
    "net_profit_owner": ["股东应占溢利"],
    "eps_basic": ["每股基本盈利"],
    "dps": ["每股股息"],
    "dividend_total": ["股息"],
    "asset_sale_gain": ["出售资产之溢利"],
    "fair_value_gain": ["重估盈余", "按公平价值列账的金融资产公平值增加/减少"],
    "other_gains": ["其他收益", "其他收入"],
    "impairment": ["减值及拨备"],
}

CF_ALIASES = {
    "cfo": ["经营业务现金净额"],
    "capex": ["购建固定资产", "购建无形资产及其他资产"],
    "dividend_paid": ["已付股息(融资)"],
    "buyback": ["回购股份"],
    "new_debt": ["新增借款", "发行债券"],
    "repay_debt": ["偿还借款", "赎回债券"],
}


def _num(value):
    if value in ("-", None, ""):
        return np.nan
    return pd.to_numeric(value, errors="coerce")


def _first_present(row: pd.Series, names: Iterable[str]) -> float:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return float(row[name])
    return np.nan


def _sum_present(row: pd.Series, names: Iterable[str]) -> float:
    vals = []
    for name in names:
        if name in row.index and pd.notna(row[name]):
            vals.append(float(row[name]))
    if not vals:
        return np.nan
    return float(np.nansum(vals))


def fetch_eastmoney_hk_quotes(page_size: int = 100, pause: float = 0.05) -> pd.DataFrame:
    url = "https://72.push2.eastmoney.com/api/qt/clist/get"
    fields = (
        "f2,f3,f4,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,"
        "f24,f25,f62,f115"
    )
    base = {
        "po": "1",
        "np": "2",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2",
        "fields": fields,
        "_": str(int(time.time() * 1000)),
    }
    rows = []
    total = None
    for page in range(1, 200):
        params = dict(base, pn=str(page), pz=str(page_size))
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = (resp.json().get("data") or {})
        diff = data.get("diff") or {}
        if not diff:
            break
        rows.extend(diff.values())
        total = data.get("total") or total
        if total and len(rows) >= int(total):
            break
        time.sleep(pause)
    raw = pd.DataFrame(rows)
    if raw.empty:
        return raw
    df = pd.DataFrame(
        {
            "symbol": raw["f12"].astype(str).str.zfill(5),
            "em_name": raw["f14"],
            "price_hkd": raw["f2"].map(_num),
            "pct_chg": raw["f3"].map(_num),
            "volume_shares": raw["f5"].map(_num),
            "turnover_hkd": raw["f6"].map(_num),
            "turnover_rate_pct": raw["f8"].map(_num),
            "pe_dynamic": raw["f9"].map(_num),
            "pb": raw["f23"].map(_num),
            "pe_ttm": raw["f115"].map(_num),
            "total_mv_hkd": raw["f20"].map(_num),
            "float_mv_hkd": raw["f21"].map(_num),
            "latest_high": raw["f15"].map(_num),
            "latest_low": raw["f16"].map(_num),
            "latest_open": raw["f17"].map(_num),
            "prev_close": raw["f18"].map(_num),
        }
    )
    df["ts_code"] = df["symbol"] + ".HK"
    return df.sort_values("turnover_hkd", ascending=False).drop_duplicates("ts_code", keep="first")


def latest_tushare_daily_amount(pro, lookback_open_days: int = 20) -> tuple[pd.DataFrame, str]:
    today = datetime.now().strftime("%Y%m%d")
    start = (pd.Timestamp.today() - pd.Timedelta(days=60)).strftime("%Y%m%d")
    cal = pro.query("hk_tradecal", start_date=start, end_date=today)
    open_days = sorted(
        cal.loc[cal["is_open"].astype(str).isin(["1", "True", "true"]), "cal_date"].tolist(),
        reverse=True,
    )
    frames = []
    latest_populated = None
    for d in open_days[: lookback_open_days + 8]:
        daily = pro.query("hk_daily", trade_date=d)
        if daily.empty:
            continue
        if latest_populated is None:
            latest_populated = d
        frames.append(daily[["ts_code", "trade_date", "close", "vol", "amount"]])
        if len(frames) >= lookback_open_days:
            break
        time.sleep(0.05)
    if not frames:
        return pd.DataFrame(), ""
    raw = pd.concat(frames, ignore_index=True)
    for col in ["close", "vol", "amount"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    # Tushare HK amount is in HKD in observed data, not ten-thousands.
    agg = raw.groupby("ts_code").agg(
        ts_latest_close=("close", "first"),
        ts_days=("trade_date", "nunique"),
        ts_avg_amount_hkd=("amount", "mean"),
        ts_median_amount_hkd=("amount", "median"),
    )
    return agg.reset_index(), latest_populated or ""


def pivot_statement(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    cleaned = df.copy()
    cleaned["ind_value"] = pd.to_numeric(cleaned["ind_value"], errors="coerce")
    pivot = (
        cleaned.dropna(subset=["end_date", "ind_name"])
        .pivot_table(index="end_date", columns="ind_name", values="ind_value", aggfunc="first")
        .sort_index()
    )
    return pivot


def annual_rows(pivot: pd.DataFrame) -> pd.DataFrame:
    if pivot.empty:
        return pivot
    # Main pass: calendar-year annual rows. This is conservative for HK because
    # Tushare does not expose report_type on the HK indicator endpoints.
    dec = pivot[pivot.index.astype(str).str.endswith("1231")]
    if len(dec) >= 2:
        return dec
    # Fallback for non-December fiscal year companies: use the latest date per
    # year and accept that it requires manual verification in the final list.
    tmp = pivot.copy()
    tmp["_year"] = tmp.index.astype(str).str[:4]
    idx = tmp.groupby("_year").tail(1).index
    return pivot.loc[idx]


def pull_financials_for_code(pro, ts_code: str) -> dict[str, pd.DataFrame]:
    out = {}
    for endpoint in ("hk_balancesheet", "hk_income", "hk_cashflow"):
        try:
            df = pro.query(endpoint, ts_code=ts_code, limit=8000)
        except Exception as exc:
            out[endpoint] = pd.DataFrame()
            out[f"{endpoint}_error"] = str(exc)
            continue
        out[endpoint] = pivot_statement(df)
        time.sleep(0.04)
    return out


@dataclass
class Metrics:
    ts_code: str
    latest_report_date: str
    cash_like: float
    strict_cash_like: float
    total_liabilities: float
    interest_debt: float
    net_cash_after_all_liab: float
    cash_to_liab: float
    net_cash_to_mv: float
    revenue_latest: float
    revenue_cagr_3y: float
    profit_latest: float
    profit_cagr_3y: float
    profit_positive_years: int
    cfo_latest: float
    cfo_positive_years: int
    cfo_to_profit_latest: float
    oneoff_ratio_latest: float
    dps_latest: float
    div_paid_latest: float
    buyback_latest: float
    shareholder_return_hkd: float
    dps_yield_est: float
    dividend_paid_yield_est: float
    score: float
    flags: str


def compute_metrics(ts_code: str, fin: dict[str, pd.DataFrame], market_cap_hkd: float, price_hkd: float) -> Metrics | None:
    bs = annual_rows(fin.get("hk_balancesheet", pd.DataFrame()))
    inc = annual_rows(fin.get("hk_income", pd.DataFrame()))
    cf = annual_rows(fin.get("hk_cashflow", pd.DataFrame()))
    if bs.empty or inc.empty:
        return None

    latest_date = max(bs.index[-1], inc.index[-1])
    bs_row = bs.iloc[-1]
    inc_row = inc.iloc[-1]
    cf_row = cf.iloc[-1] if not cf.empty else pd.Series(dtype=float)

    cash = _first_present(bs_row, BS_ALIASES["cash"])
    short_deposit = _sum_present(bs_row, BS_ALIASES["short_deposit"])
    short_invest = _sum_present(bs_row, BS_ALIASES["short_invest"])
    restricted_cash = _sum_present(bs_row, BS_ALIASES["restricted_cash"])
    total_liab = _first_present(bs_row, BS_ALIASES["total_liabilities"])
    if pd.isna(total_liab):
        cur = _first_present(bs_row, BS_ALIASES["current_liabilities"])
        noncur = _first_present(bs_row, BS_ALIASES["noncurrent_liabilities"])
        total_liab = np.nansum([cur, noncur]) if pd.notna(cur) or pd.notna(noncur) else np.nan

    short_debt = _sum_present(bs_row, BS_ALIASES["short_debt"])
    long_debt = _sum_present(bs_row, BS_ALIASES["long_debt"])
    lease_current = _sum_present(bs_row, BS_ALIASES["lease_current"])
    lease_noncurrent = _sum_present(bs_row, BS_ALIASES["lease_noncurrent"])
    debt_parts = [short_debt, long_debt, lease_current, lease_noncurrent]
    interest_debt = np.nansum([x for x in debt_parts if pd.notna(x)]) if any(pd.notna(x) for x in debt_parts) else np.nan

    strict_cash_like = np.nansum([x for x in [cash, short_deposit] if pd.notna(x)])
    cash_like = np.nansum([x for x in [cash, short_deposit, short_invest] if pd.notna(x)])
    if pd.isna(cash) and pd.isna(short_deposit) and pd.isna(short_invest):
        cash_like = np.nan
        strict_cash_like = np.nan
    net_cash_after_all_liab = cash_like - total_liab if pd.notna(cash_like) and pd.notna(total_liab) else np.nan
    cash_to_liab = cash_like / total_liab if pd.notna(total_liab) and total_liab else np.nan
    net_cash_to_mv = net_cash_after_all_liab / market_cap_hkd if pd.notna(net_cash_after_all_liab) and market_cap_hkd else np.nan

    revenue_series = inc.apply(lambda r: _first_present(r, INCOME_ALIASES["revenue"]), axis=1).dropna()
    profit_series = inc.apply(lambda r: _first_present(r, INCOME_ALIASES["net_profit_owner"]), axis=1).dropna()
    cfo_series = cf.apply(lambda r: _first_present(r, CF_ALIASES["cfo"]), axis=1).dropna() if not cf.empty else pd.Series(dtype=float)

    revenue_latest = float(revenue_series.iloc[-1]) if not revenue_series.empty else np.nan
    profit_latest = float(profit_series.iloc[-1]) if not profit_series.empty else np.nan
    cfo_latest = float(cfo_series.iloc[-1]) if not cfo_series.empty else np.nan

    def cagr(series: pd.Series, years: int = 3) -> float:
        if len(series) < 2:
            return np.nan
        sub = series.tail(years + 1)
        first = float(sub.iloc[0])
        last = float(sub.iloc[-1])
        n = len(sub) - 1
        if first <= 0 or last <= 0 or n <= 0:
            return np.nan
        return (last / first) ** (1 / n) - 1

    revenue_cagr_3y = cagr(revenue_series)
    profit_cagr_3y = cagr(profit_series)
    profit_positive_years = int((profit_series.tail(4) > 0).sum()) if not profit_series.empty else 0
    cfo_positive_years = int((cfo_series.tail(4) > 0).sum()) if not cfo_series.empty else 0
    cfo_to_profit = cfo_latest / profit_latest if pd.notna(cfo_latest) and profit_latest and profit_latest > 0 else np.nan

    asset_sale = _sum_present(inc_row, INCOME_ALIASES["asset_sale_gain"])
    fair_value = _sum_present(inc_row, INCOME_ALIASES["fair_value_gain"])
    other_gains = _sum_present(inc_row, INCOME_ALIASES["other_gains"])
    oneoff = np.nansum([x for x in [asset_sale, fair_value, other_gains] if pd.notna(x)])
    oneoff_ratio = oneoff / abs(profit_latest) if pd.notna(profit_latest) and profit_latest else np.nan

    dps = _first_present(inc_row, INCOME_ALIASES["dps"])
    div_total = _first_present(inc_row, INCOME_ALIASES["dividend_total"])
    div_paid = abs(_first_present(cf_row, CF_ALIASES["dividend_paid"])) if not cf.empty else np.nan
    buyback = abs(_first_present(cf_row, CF_ALIASES["buyback"])) if not cf.empty else np.nan
    shareholder_return = np.nansum([x for x in [div_paid, buyback] if pd.notna(x)])
    dps_yield = dps / price_hkd if pd.notna(dps) and price_hkd else np.nan
    dividend_paid_yield = shareholder_return / market_cap_hkd if pd.notna(shareholder_return) and market_cap_hkd else np.nan

    flags = []
    if pd.notna(cash_to_liab) and cash_to_liab >= 1:
        flags.append("现金类资产>总负债")
    elif pd.notna(interest_debt) and pd.notna(cash_like) and cash_like > interest_debt:
        flags.append("现金类资产>有息负债")
    else:
        flags.append("非严格净现金")
    if profit_positive_years >= 3:
        flags.append("近年盈利稳定")
    if cfo_positive_years >= 3:
        flags.append("经营现金流稳定")
    if pd.notna(oneoff_ratio) and oneoff_ratio > 0.5:
        flags.append("一次性/其他收益占比较高")
    if pd.notna(dividend_paid_yield) and dividend_paid_yield >= 0.04:
        flags.append("股东回报率较高")

    score = 0.0
    if pd.notna(cash_to_liab):
        score += min(max(cash_to_liab, 0), 2.0) * 18
    if pd.notna(net_cash_to_mv):
        score += min(max(net_cash_to_mv, -1), 1.5) * 16
    if profit_positive_years:
        score += profit_positive_years * 5
    if cfo_positive_years:
        score += cfo_positive_years * 5
    if pd.notna(cfo_to_profit):
        score += min(max(cfo_to_profit, 0), 2.0) * 8
    if pd.notna(dividend_paid_yield):
        score += min(max(dividend_paid_yield, 0), 0.12) * 300
    elif pd.notna(dps_yield):
        score += min(max(dps_yield, 0), 0.12) * 220
    if pd.notna(profit_cagr_3y):
        score += min(max(profit_cagr_3y, -0.4), 0.4) * 20
    if pd.notna(oneoff_ratio):
        score -= min(max(oneoff_ratio, 0), 1.5) * 12

    return Metrics(
        ts_code=ts_code,
        latest_report_date=str(latest_date),
        cash_like=cash_like,
        strict_cash_like=strict_cash_like,
        total_liabilities=total_liab,
        interest_debt=interest_debt,
        net_cash_after_all_liab=net_cash_after_all_liab,
        cash_to_liab=cash_to_liab,
        net_cash_to_mv=net_cash_to_mv,
        revenue_latest=revenue_latest,
        revenue_cagr_3y=revenue_cagr_3y,
        profit_latest=profit_latest,
        profit_cagr_3y=profit_cagr_3y,
        profit_positive_years=profit_positive_years,
        cfo_latest=cfo_latest,
        cfo_positive_years=cfo_positive_years,
        cfo_to_profit_latest=cfo_to_profit,
        oneoff_ratio_latest=oneoff_ratio,
        dps_latest=dps,
        div_paid_latest=div_paid,
        buyback_latest=buyback,
        shareholder_return_hkd=shareholder_return,
        dps_yield_est=dps_yield,
        dividend_paid_yield_est=dividend_paid_yield,
        score=score,
        flags=";".join(flags),
    )


def build_prefilter(universe: pd.DataFrame) -> pd.DataFrame:
    df = universe.copy()
    for col in ["total_mv_hkd", "turnover_hkd", "pe_ttm", "pe_dynamic", "pb", "price_hkd"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    name = df["name"].fillna(df["em_name"]).fillna("")
    bad = name.str.upper().apply(lambda x: any(part.upper() in x for part in BAD_NAME_PARTS))
    pe = df["pe_ttm"].where(df["pe_ttm"].notna(), df["pe_dynamic"])
    df["prefilter_pe"] = pe
    mask = (
        df["price_hkd"].gt(0)
        & df["total_mv_hkd"].between(2e8, 8e10)
        & df["pb"].between(0.05, 1.8)
        & ((pe.between(0.5, 25)) | pe.isna())
        & df["turnover_hkd"].fillna(0).between(5e4, 1.5e8)
        & ~bad
    )
    out = df.loc[mask].copy()
    # Favor the exact style described: smaller market cap, lower PB, low-ish liquidity.
    out["prefilter_rank"] = (
        out["pb"].rank(pct=True, ascending=True).fillna(0.7) * 0.35
        + out["total_mv_hkd"].rank(pct=True, ascending=True).fillna(0.7) * 0.30
        + out["turnover_hkd"].rank(pct=True, ascending=True).fillna(0.7) * 0.20
        + out["prefilter_pe"].rank(pct=True, ascending=True).fillna(0.7) * 0.15
    )
    return out.sort_values("prefilter_rank")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-codes", type=int, default=260, help="max prefiltered codes for Tushare financial pulls")
    parser.add_argument("--resume", action="store_true", help="reuse existing financial cache where available")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pro = ts.pro_api()

    print("Fetching Tushare HK basic universe...")
    basic = pro.query("hk_basic", list_status="L")
    basic.to_csv(OUT_DIR / "tushare_hk_basic.csv", index=False)

    print("Fetching Eastmoney HK quotes...")
    spot_cache = OUT_DIR / "eastmoney_hk_quotes.csv"
    try:
        spot = fetch_eastmoney_hk_quotes()
        spot.to_csv(spot_cache, index=False)
    except Exception as exc:
        if not spot_cache.exists():
            raise
        print(f"Eastmoney quote refresh failed, using cache: {exc}")
        spot = pd.read_csv(spot_cache)

    print("Fetching Tushare recent HK daily liquidity...")
    daily_cache = OUT_DIR / "tushare_hk_recent_liquidity.csv"
    try:
        daily_liq, latest_tushare_daily = latest_tushare_daily_amount(pro)
        if not daily_liq.empty:
            daily_liq.to_csv(daily_cache, index=False)
    except Exception as exc:
        if not daily_cache.exists():
            raise
        print(f"Tushare daily liquidity refresh failed, using cache: {exc}")
        daily_liq = pd.read_csv(daily_cache)
        latest_tushare_daily = "cached"

    universe = basic.drop_duplicates("ts_code").merge(spot, on="ts_code", how="inner")
    if not daily_liq.empty:
        universe = universe.merge(daily_liq, on="ts_code", how="left")
    universe = universe.drop_duplicates("ts_code")
    universe.to_csv(OUT_DIR / "hk_merged_universe.csv", index=False)

    pre = build_prefilter(universe)
    pre.to_csv(OUT_DIR / "hk_prefilter.csv", index=False)
    selected = pre.drop_duplicates("ts_code").head(args.max_codes).copy()
    print(f"Universe: {len(universe)}, prefilter: {len(pre)}, pulling financials: {len(selected)}")

    metrics_rows = []
    cache_dir = OUT_DIR / "financial_cache"
    cache_dir.mkdir(exist_ok=True)
    for idx, row in selected.reset_index(drop=True).iterrows():
        code = row["ts_code"]
        cache_path = cache_dir / f"{code.replace('.', '_')}.json"
        print(f"[{idx + 1}/{len(selected)}] {code} {row.get('name') or row.get('em_name')}")
        if args.resume and cache_path.exists():
            cache_obj = json.loads(cache_path.read_text())
            fin = {}
            for ep in ("hk_balancesheet", "hk_income", "hk_cashflow"):
                payload = cache_obj.get(ep, {})
                fin[ep] = pd.DataFrame(payload.get("data", []))
                if not fin[ep].empty:
                    fin[ep] = fin[ep].set_index("end_date")
                    fin[ep].columns.name = "ind_name"
        else:
            fin = pull_financials_for_code(pro, code)
            cache_obj = {}
            for ep in ("hk_balancesheet", "hk_income", "hk_cashflow"):
                df = fin.get(ep, pd.DataFrame())
                cache_obj[ep] = {"data": df.reset_index().to_dict(orient="records") if not df.empty else []}
            cache_path.write_text(json.dumps(cache_obj, ensure_ascii=False))
        metric = compute_metrics(code, fin, row["total_mv_hkd"], row["price_hkd"])
        if metric is None:
            continue
        m = metric.__dict__
        for key in ["name", "fullname", "market", "list_date", "price_hkd", "total_mv_hkd", "float_mv_hkd", "turnover_hkd", "pb", "pe_ttm", "pe_dynamic", "ts_avg_amount_hkd", "ts_median_amount_hkd"]:
            m[key] = row.get(key)
        metrics_rows.append(m)

    metrics = pd.DataFrame(metrics_rows)
    if metrics.empty:
        raise SystemExit("No metrics produced")

    # Hard-gate after financial pulls: keep companies that at least cover
    # interest-bearing debt, and rank the stricter all-liabilities names first.
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

    top_cols = [
        "ts_code",
        "name",
        "market",
        "price_hkd",
        "total_mv_hkd",
        "turnover_hkd",
        "pb",
        "pe_ttm",
        "latest_report_date",
        "cash_to_liab",
        "net_cash_to_mv",
        "profit_latest",
        "profit_positive_years",
        "cfo_to_profit_latest",
        "oneoff_ratio_latest",
        "dividend_paid_yield_est",
        "dps_yield_est",
        "score",
        "liquidity_bucket",
        "flags",
    ]
    md = ranked.head(30)[top_cols].copy()
    for c in ["total_mv_hkd", "turnover_hkd", "profit_latest"]:
        md[c] = (md[c] / 1e8).round(2)
    pct_cols = ["cash_to_liab", "net_cash_to_mv", "cfo_to_profit_latest", "oneoff_ratio_latest", "dividend_paid_yield_est", "dps_yield_est"]
    for c in pct_cols:
        md[c] = md[c].map(lambda x: "" if pd.isna(x) else f"{x:.1%}")
    md["score"] = md["score"].round(1)
    md.rename(
        columns={
            "total_mv_hkd": "总市值(亿HKD)",
            "turnover_hkd": "最新成交额(亿HKD)",
            "profit_latest": "最新年股东利润(亿)",
        },
        inplace=True,
    )
    report = [
        f"# 港股净现金/股东回报筛选结果",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Tushare 港股日线最近有数据日期: {latest_tushare_daily or 'N/A'}",
        f"- Tushare 上市港股: {len(basic)}",
        f"- 可与东方财富行情匹配: {len(universe)}",
        f"- 初筛数量: {len(pre)}",
        f"- 拉取 Tushare 财务数量: {len(selected)}",
        f"- 通过财务硬门槛数量: {len(ranked)}",
        "",
        "说明: 金额单位若无特别说明为公司报表原币；总市值/成交额来自东方财富港股行情，单位为港币。",
        "Tushare 港股指标接口不直接给 report_type，因此年报初筛优先使用 12-31 行，非 12-31 财年公司需要逐个核实。",
        "",
        df_to_markdown(md, index=False),
        "",
    ]
    (OUT_DIR / "hk_ranked_candidates.md").write_text("\n".join(report))
    print(f"Wrote {OUT_DIR / 'hk_ranked_candidates.csv'}")
    print(f"Wrote {OUT_DIR / 'hk_ranked_candidates.md'}")


if __name__ == "__main__":
    main()
