#!/usr/bin/env python3
"""Build a watchlist for high-dividend HK names that static PE gates miss."""

from __future__ import annotations

import html
import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

from hk_markdown import df_to_markdown
from hk_netcash_screen import (
    BAD_NAME_PARTS,
    BS_ALIASES,
    CF_ALIASES,
    INCOME_ALIASES,
    OUT_DIR,
    _first_present,
    _sum_present,
    compute_metrics,
    pull_financials_for_code,
)


DEFAULT_PUBLIC_DIR = Path(__file__).resolve().parents[1]
PUBLIC = Path(os.environ.get("HK_NETCASH_PUBLIC_DIR", DEFAULT_PUBLIC_DIR)).resolve()
MAX_PULLS = int(os.environ.get("HK_SPECIAL_DIVIDEND_MAX_PULLS", "180"))
FOCUS_CODES = {"01023.HK"}


def is_missing(value) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False


def esc(value) -> str:
    if is_missing(value):
        return ""
    return html.escape(str(value), quote=True)


def yi(value) -> str:
    if is_missing(value):
        return ""
    return f"{float(value) / 1e8:.2f}亿"


def pct(value) -> str:
    if is_missing(value):
        return ""
    return f"{float(value) * 100:.1f}%"


def fmt(value, digits: int = 2) -> str:
    if is_missing(value):
        return ""
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.{digits}f}"


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


def metric_series(df: pd.DataFrame, aliases: list[str]) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    return df.apply(lambda row: _first_present(row, aliases), axis=1).dropna()


def detect_fiscal_mmdd(fin: dict[str, pd.DataFrame]) -> str:
    """Infer the annual fiscal period by preferring the richest repeated period."""
    inc = fin.get("hk_income", pd.DataFrame())
    if inc.empty:
        return "1231"
    idx = inc.index.astype(str)
    mmdds = sorted({x[-4:] for x in idx if len(x) >= 8})
    candidates: list[dict] = []
    revenue = metric_series(inc, INCOME_ALIASES["revenue"])
    for mmdd in mmdds:
        rows = revenue[revenue.index.astype(str).str.endswith(mmdd)]
        if len(rows) < 2:
            continue
        candidates.append(
            {
                "mmdd": mmdd,
                "count": len(rows),
                "median_revenue": float(rows.tail(5).median()) if not rows.empty else 0.0,
                "last_date": max(rows.index.astype(str)),
            }
        )
    if not candidates:
        return "1231" if "1231" in mmdds else (mmdds[-1] if mmdds else "1231")
    chosen = sorted(candidates, key=lambda x: (x["median_revenue"], x["count"], x["last_date"]), reverse=True)[0]
    return str(chosen["mmdd"])


def filter_fiscal_year(fin: dict[str, pd.DataFrame], mmdd: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for ep in ("hk_balancesheet", "hk_income", "hk_cashflow"):
        df = fin.get(ep, pd.DataFrame())
        if df.empty:
            out[ep] = df
            continue
        out[ep] = df[df.index.astype(str).str.endswith(mmdd)].copy()
    return out


def annual_detail(fin: dict[str, pd.DataFrame]) -> pd.DataFrame:
    bs = fin.get("hk_balancesheet", pd.DataFrame()).tail(5)
    inc = fin.get("hk_income", pd.DataFrame()).tail(5)
    cf = fin.get("hk_cashflow", pd.DataFrame()).tail(5)
    rows = []
    for idx in sorted(set(bs.index).union(inc.index).union(cf.index)):
        b = bs.loc[idx] if idx in bs.index else pd.Series(dtype=float)
        i = inc.loc[idx] if idx in inc.index else pd.Series(dtype=float)
        c = cf.loc[idx] if idx in cf.index else pd.Series(dtype=float)
        profit = _first_present(i, INCOME_ALIASES["net_profit_owner"])
        cfo = _first_present(c, CF_ALIASES["cfo"])
        div = abs(_first_present(c, CF_ALIASES["dividend_paid"])) if not c.empty else np.nan
        buyback = abs(_first_present(c, CF_ALIASES["buyback"])) if not c.empty else np.nan
        cash = _first_present(b, BS_ALIASES["cash"])
        short_deposit = _sum_present(b, BS_ALIASES["short_deposit"])
        short_invest = _sum_present(b, BS_ALIASES["short_invest"])
        cash_like = np.nansum([x for x in [cash, short_deposit, short_invest] if pd.notna(x)])
        total_liab = _first_present(b, BS_ALIASES["total_liabilities"])
        rows.append(
            {
                "end_date": idx,
                "revenue": _first_present(i, INCOME_ALIASES["revenue"]),
                "profit_owner": profit,
                "cfo": cfo,
                "dividend_paid": div,
                "buyback": buyback,
                "shareholder_return": np.nansum([x for x in [div, buyback] if pd.notna(x)]),
                "cash_like": cash_like,
                "total_liabilities": total_liab,
                "cash_to_liab": cash_like / total_liab if pd.notna(total_liab) and total_liab else np.nan,
                "cfo_to_profit": cfo / profit if pd.notna(cfo) and pd.notna(profit) and profit else np.nan,
            }
        )
    return pd.DataFrame(rows)


def prepare_universe() -> pd.DataFrame:
    universe = pd.read_csv(OUT_DIR / "hk_merged_universe.csv").drop_duplicates("ts_code")
    for col in [
        "price_hkd",
        "total_mv_hkd",
        "turnover_hkd",
        "ts_avg_amount_hkd",
        "ts_median_amount_hkd",
        "pb",
        "pe_ttm",
        "pe_dynamic",
    ]:
        if col not in universe.columns:
            universe[col] = np.nan
        universe[col] = pd.to_numeric(universe[col], errors="coerce")
    universe["liquidity_ref_hkd"] = universe[["turnover_hkd", "ts_median_amount_hkd", "ts_avg_amount_hkd"]].max(axis=1, skipna=True)
    pe = universe["pe_ttm"].where(universe["pe_ttm"].notna(), universe["pe_dynamic"])
    universe["prefilter_pe"] = pe
    name = universe["name"].fillna(universe.get("em_name")).fillna("")
    bad = name.str.upper().apply(lambda x: any(part.upper() in x for part in BAD_NAME_PARTS))
    mask = (
        universe["price_hkd"].gt(0)
        & universe["total_mv_hkd"].between(2e8, 2e10)
        & universe["pb"].between(0.05, 1.3)
        & universe["liquidity_ref_hkd"].fillna(0).between(5e4, 2e8)
        & ~bad
    )
    candidates = universe.loc[mask].copy()
    candidates["static_exclusion_hint"] = np.select(
        [
            candidates["prefilter_pe"].le(0),
            candidates["prefilter_pe"].isna(),
            candidates["prefilter_pe"].gt(25),
        ],
        ["负PE/亏损口径", "PE缺失", "PE偏高"],
        default="低PB高分红旁路",
    )
    candidates["prefilter_rank"] = (
        candidates["pb"].rank(pct=True, ascending=True).fillna(0.7) * 0.35
        + candidates["total_mv_hkd"].rank(pct=True, ascending=True).fillna(0.7) * 0.20
        + candidates["liquidity_ref_hkd"].rank(pct=True, ascending=True).fillna(0.7) * 0.15
        + candidates["prefilter_pe"].rank(pct=True, ascending=True).fillna(0.7) * 0.15
        + candidates["prefilter_pe"].le(0).astype(float) * 0.15
    )
    top = candidates.sort_values("prefilter_rank").head(MAX_PULLS)
    focus = candidates.loc[candidates["ts_code"].isin(FOCUS_CODES)]
    return pd.concat([top, focus], ignore_index=True).drop_duplicates("ts_code", keep="last")


def quality_label(row: pd.Series) -> str:
    if row["profit_latest"] > 0 and row["cfo_latest"] > 0 and row["profit_positive_years"] >= 3:
        return "可复核"
    if row["profit_latest"] <= 0 and row["cfo_latest"] > 0 and row["profit_positive_years"] >= 2:
        return "反转观察"
    if row["profit_latest"] > 0 and row["cfo_latest"] <= 0:
        return "现金流待验证"
    return "谨慎观察"


def watch_score(row: pd.Series) -> float:
    def val(name: str, default: float = 0.0) -> float:
        x = row.get(name, default)
        if pd.isna(x):
            return default
        return float(x)

    score = 0.0
    score += min(max(val("dividend_paid_yield_est"), 0), 0.18) / 0.18 * 35
    score += min(max(val("cash_to_liab"), 0), 2.0) / 2.0 * 18
    score += min(max(val("net_cash_to_mv"), -0.5), 1.0) * 12
    score += max(0, 1.3 - val("pb", 1.3)) / 1.25 * 10
    score += min(max(val("profit_positive_years"), 0), 4) / 4 * 8
    score += min(max(val("cfo_positive_years"), 0), 4) / 4 * 8
    score += min(max(np.log10(max(val("liquidity_ref_hkd", 1), 1)) - 4.7, 0), 2.5) / 2.5 * 5
    if val("profit_latest") <= 0:
        score -= 8
    if val("cfo_latest") <= 0:
        score -= 8
    if val("oneoff_ratio_latest") > 0.5:
        score -= min(val("oneoff_ratio_latest"), 1.5) * 6
    if row.get("annual_mmdd") != "1231":
        score -= 2
    return float(score)


def build_watchlist() -> tuple[pd.DataFrame, pd.DataFrame]:
    selected = prepare_universe()
    selected.to_csv(OUT_DIR / "special_dividend_prefilter.csv", index=False)
    cache_dir = OUT_DIR / "financial_cache"
    cache_dir.mkdir(exist_ok=True)
    pro = ts.pro_api()
    rows = []
    detail_rows = []
    pulled = 0

    for idx, row in selected.reset_index(drop=True).iterrows():
        code = row["ts_code"]
        cache_path = cache_dir / f"{code.replace('.', '_')}.json"
        if cache_path.exists():
            fin = load_cached_financials(cache_path)
            source = "cache"
        else:
            print(f"[pull {pulled + 1}] {idx + 1}/{len(selected)} {code} {row.get('name')}")
            fin = pull_financials_for_code(pro, code)
            save_cached_financials(cache_path, fin)
            pulled += 1
            source = "new"
            time.sleep(0.04)
        mmdd = detect_fiscal_mmdd(fin)
        annual_fin = filter_fiscal_year(fin, mmdd)
        metric = compute_metrics(code, annual_fin, row["total_mv_hkd"], row["price_hkd"])
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
            "liquidity_ref_hkd",
            "pb",
            "pe_ttm",
            "pe_dynamic",
            "prefilter_pe",
            "static_exclusion_hint",
        ]:
            m[key] = row.get(key)
        m["annual_mmdd"] = mmdd
        m["annual_period"] = f"FY-{mmdd[:2]}-{mmdd[2:]}"
        m["financial_source"] = source
        m["special_label"] = quality_label(pd.Series(m))
        m["watch_score"] = watch_score(pd.Series(m))
        rows.append(m)
        detail = annual_detail(annual_fin)
        if not detail.empty:
            detail["ts_code"] = code
            detail["name"] = row.get("name")
            detail_rows.extend(detail.to_dict(orient="records"))

    all_scored = pd.DataFrame(rows)
    if all_scored.empty:
        raise SystemExit("No special dividend candidates scored")
    all_scored.sort_values("watch_score", ascending=False).to_csv(OUT_DIR / "special_dividend_all_scored.csv", index=False)
    details = pd.DataFrame(detail_rows)
    if not details.empty:
        details.to_csv(OUT_DIR / "special_dividend_annuals.csv", index=False)

    primary_path = OUT_DIR / "hk_ranked_candidates.csv"
    primary_codes = set()
    if primary_path.exists():
        primary_codes = set(pd.read_csv(primary_path, usecols=["ts_code"])["ts_code"].astype(str))

    mask = (
        (all_scored["dividend_paid_yield_est"].fillna(0) >= 0.06)
        & (all_scored["cash_like"] > all_scored["interest_debt"].fillna(0))
        & (all_scored["cash_to_liab"].fillna(0) >= 0.8)
        & (all_scored["profit_positive_years"] >= 2)
        & (all_scored["cfo_positive_years"] >= 2)
        & (all_scored["latest_report_date"].astype(str) >= "20240630")
        & ~all_scored["ts_code"].astype(str).isin(primary_codes)
    )
    watch = all_scored.loc[mask].copy()
    watch.sort_values("watch_score", ascending=False, inplace=True)
    watch.insert(0, "rank", range(1, len(watch) + 1))
    watch.to_csv(OUT_DIR / "special_dividend_watchlist.csv", index=False)
    print(f"Special prefilter: {len(selected)}")
    print(f"Special scored: {len(all_scored)}")
    print(f"Special watchlist: {len(watch)}")
    print(f"Pulled new financials: {pulled}")
    return watch, details


def render_html(df: pd.DataFrame, generated: str) -> str:
    cards = []
    for _, row in df.head(30).iterrows():
        cards.append(
            f"""
<article class="card">
  <div class="head"><b>#{int(row['rank'])}</b><span>{esc(row['ts_code'])} {esc(row['name'])}</span><em>{fmt(row['watch_score'], 1)}</em></div>
  <div class="grid">
    <div><label>标签</label><strong>{esc(row['special_label'])}</strong></div>
    <div><label>静态漏斗</label><strong>{esc(row['static_exclusion_hint'])}</strong></div>
    <div><label>市值/PB</label><strong>{yi(row['total_mv_hkd'])} / {fmt(row['pb'])}</strong></div>
    <div><label>股东回报率</label><strong>{pct(row['dividend_paid_yield_est'])}</strong></div>
    <div><label>现金/负债</label><strong>{pct(row['cash_to_liab'])}</strong></div>
    <div><label>净现金/市值</label><strong>{pct(row['net_cash_to_mv'])}</strong></div>
    <div><label>近4年盈利/CFO</label><strong>{int(row['profit_positive_years'])}/4 · {int(row['cfo_positive_years'])}/4</strong></div>
    <div><label>最新利润/CFO</label><strong>{yi(row['profit_latest'])} / {yi(row['cfo_latest'])}</strong></div>
  </div>
  <p>{esc(row['flags'])}；年结日 {esc(row['annual_period'])}；最新报告期 {esc(row['latest_report_date'])}</p>
</article>"""
        )
    table = df.head(120)[
        [
            "rank",
            "ts_code",
            "name",
            "special_label",
            "static_exclusion_hint",
            "price_hkd",
            "total_mv_hkd",
            "liquidity_ref_hkd",
            "pb",
            "prefilter_pe",
            "cash_to_liab",
            "net_cash_to_mv",
            "profit_latest",
            "cfo_latest",
            "profit_positive_years",
            "cfo_positive_years",
            "dividend_paid_yield_est",
            "watch_score",
            "flags",
        ]
    ].copy()
    for col in ["total_mv_hkd", "liquidity_ref_hkd", "profit_latest", "cfo_latest"]:
        table[col] = table[col].map(yi)
    for col in ["cash_to_liab", "net_cash_to_mv", "dividend_paid_yield_est"]:
        table[col] = table[col].map(pct)
    for col in ["price_hkd", "pb", "prefilter_pe", "watch_score"]:
        table[col] = table[col].map(lambda x: fmt(x, 3 if col != "watch_score" else 1))
    table.rename(
        columns={
            "rank": "排名",
            "ts_code": "代码",
            "name": "名称",
            "special_label": "标签",
            "static_exclusion_hint": "漏斗原因",
            "price_hkd": "价格",
            "total_mv_hkd": "市值",
            "liquidity_ref_hkd": "流动性参考",
            "pb": "PB",
            "prefilter_pe": "PE",
            "cash_to_liab": "现金/负债",
            "net_cash_to_mv": "净现金/市值",
            "profit_latest": "最新利润",
            "cfo_latest": "最新CFO",
            "profit_positive_years": "盈利年数",
            "cfo_positive_years": "CFO年数",
            "dividend_paid_yield_est": "股东回报率",
            "watch_score": "观察分",
            "flags": "标签说明",
        },
        inplace=True,
    )
    table_html = table.to_html(index=False, escape=True, classes="rank-table")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>特殊高分红观察池</title>
<style>
body{{margin:0;background:#f7f8fa;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',Arial,sans-serif}}
header{{position:sticky;top:0;background:rgba(247,248,250,.95);backdrop-filter:blur(10px);border-bottom:1px solid #dbe1e8;padding:12px 14px;z-index:2}}
main{{max-width:1180px;margin:auto;padding:12px}}h1{{font-size:20px;margin:0 0 4px}}.meta{{color:#637083;font-size:13px;line-height:1.45}}.links a{{display:inline-block;margin:8px 8px 0 0;color:#0b63ce;text-decoration:none}}
.card{{background:white;border:1px solid #dbe1e8;border-radius:8px;padding:12px;margin:10px 0}}.head{{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}}.head span{{font-weight:700}}.head em{{font-style:normal;color:#0b6b3a;font-weight:700}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}}.grid div{{background:#f3f6f8;border-radius:6px;padding:8px;min-width:0}}label{{display:block;color:#637083;font-size:12px;margin-bottom:3px}}strong{{font-size:14px;overflow-wrap:anywhere}}p{{color:#3d4a5c;font-size:13px;line-height:1.5}}
.table-wrap{{overflow:auto;background:white;border:1px solid #dbe1e8;border-radius:8px;margin-top:18px}}table{{width:100%;border-collapse:collapse;font-size:13px;min-width:1280px}}th,td{{padding:8px;border-bottom:1px solid #dbe1e8;text-align:right;white-space:nowrap}}th:nth-child(2),th:nth-child(3),th:nth-child(4),th:nth-child(5),td:nth-child(2),td:nth-child(3),td:nth-child(4),td:nth-child(5){{text-align:left}}th{{position:sticky;top:0;background:#eef2f5}}
@media(min-width:780px){{.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card{{margin:0}}.grid{{grid-template-columns:repeat(4,minmax(0,1fr))}}}}
</style></head><body><header><h1>特殊高分红观察池</h1><div class="meta">生成：{generated}。用于捕捉低 PB、净现金或现金覆盖负债、高现金分红，但被负 PE、亏损、非 12 月财年或现金流波动挡在主榜外的港股。该榜单是人工复核池，不等同主榜。</div></header>
<main><div class="links"><a href="index.html">返回港股主榜</a><a href="special_dividend_watchlist.csv">CSV</a><a href="special_dividend_watchlist.md">Markdown</a><a href="special_dividend_annuals.csv">年度明细</a></div><section class="cards">{''.join(cards)}</section><section class="table-wrap">{table_html}</section></main></body></html>"""


def render_md(df: pd.DataFrame, generated: str) -> str:
    show = df.head(80)[
        [
            "rank",
            "ts_code",
            "name",
            "special_label",
            "static_exclusion_hint",
            "pb",
            "prefilter_pe",
            "cash_to_liab",
            "net_cash_to_mv",
            "profit_latest",
            "cfo_latest",
            "dividend_paid_yield_est",
            "watch_score",
            "flags",
        ]
    ].copy()
    for col in ["cash_to_liab", "net_cash_to_mv", "dividend_paid_yield_est"]:
        show[col] = show[col].map(pct)
    for col in ["profit_latest", "cfo_latest"]:
        show[col] = show[col].map(yi)
    return "\n".join(
        [
            "# 特殊高分红观察池",
            "",
            f"- 生成时间: {generated}",
            "- 用途: 捕捉低 PB、净现金或现金覆盖负债、高现金分红，但被负 PE、亏损、非 12 月财年或现金流波动挡在主榜外的港股。",
            "- 口径: 对非 12 月财年自动选择收入规模最大的重复期末作为年结日，再按同一套 Tushare 三表指标重算。",
            "- 注意: 这是人工复核池，不等同主榜；盈利恢复、分红持续性和治理风险必须二次验证。",
            "",
            df_to_markdown(show, index=False),
        ]
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    watch, _details = build_watchlist()
    generated = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S CST")
    (OUT_DIR / "special_dividend_watchlist.md").write_text(render_md(watch, generated), encoding="utf-8")
    (OUT_DIR / "special_dividend_watchlist.html").write_text(render_html(watch, generated), encoding="utf-8")
    print(OUT_DIR / "special_dividend_watchlist.csv")
    print(OUT_DIR / "special_dividend_watchlist.html")


if __name__ == "__main__":
    main()
