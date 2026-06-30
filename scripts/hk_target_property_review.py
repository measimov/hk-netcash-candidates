#!/usr/bin/env python3
"""Build a focused review for property-management candidates."""

from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from hk_markdown import df_to_markdown
from hk_netcash_screen import BS_ALIASES, CF_ALIASES, INCOME_ALIASES, _first_present, _sum_present, annual_rows


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve()
PUBLIC = Path(os.environ.get("HK_NETCASH_PUBLIC_DIR", OUT)).resolve()
CODES = ["01995.HK", "02156.HK"]


def pct(x) -> str:
    if pd.isna(x) or math.isinf(float(x)):
        return ""
    return f"{float(x) * 100:.1f}%"


def yi(x) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x) / 1e8:.2f}"


def esc(x) -> str:
    if pd.isna(x):
        return ""
    return html.escape(str(x), quote=True)


def rank_text(x) -> str:
    if pd.isna(x):
        return "未入池"
    return str(int(x))


def load(code: str, ep: str) -> pd.DataFrame:
    path = OUT / "financial_cache" / f"{code.replace('.', '_')}.json"
    obj = json.loads(path.read_text())
    df = pd.DataFrame(obj.get(ep, {}).get("data", []))
    if df.empty:
        return df
    df = df.set_index("end_date")
    df.columns.name = "ind_name"
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index()


def annual_trend(code: str) -> pd.DataFrame:
    bs = annual_rows(load(code, "hk_balancesheet")).tail(4)
    inc = annual_rows(load(code, "hk_income")).tail(4)
    cf = annual_rows(load(code, "hk_cashflow")).tail(4)
    years = sorted(set(bs.index).intersection(inc.index).intersection(cf.index))
    rows = []
    for y in years:
        b, i, c = bs.loc[y], inc.loc[y], cf.loc[y]
        cash = _first_present(b, BS_ALIASES["cash"])
        short_deposit = _sum_present(b, BS_ALIASES["short_deposit"])
        short_invest = _sum_present(b, BS_ALIASES["short_invest"])
        cash_like = np.nansum([x for x in [cash, short_deposit, short_invest] if pd.notna(x)])
        total_liab = _first_present(b, BS_ALIASES["total_liabilities"])
        interest_debt = _sum_present(b, BS_ALIASES["short_debt"]) + _sum_present(b, BS_ALIASES["long_debt"])
        revenue = _first_present(i, INCOME_ALIASES["revenue"])
        profit = _first_present(i, INCOME_ALIASES["net_profit_owner"])
        cfo = _first_present(c, CF_ALIASES["cfo"])
        dividend_paid = abs(_first_present(c, CF_ALIASES["dividend_paid"]))
        buyback = abs(_first_present(c, CF_ALIASES["buyback"]))
        oneoff = _sum_present(i, INCOME_ALIASES["asset_sale_gain"]) + _sum_present(i, INCOME_ALIASES["fair_value_gain"]) + _sum_present(i, INCOME_ALIASES["other_gains"])
        rows.append(
            {
                "ts_code": code,
                "end_date": y,
                "revenue": revenue,
                "profit_owner": profit,
                "cfo": cfo,
                "dividend_paid": dividend_paid,
                "buyback": buyback,
                "oneoff": oneoff,
                "oneoff_to_profit": oneoff / abs(profit) if profit else np.nan,
                "cash_like": cash_like,
                "total_liabilities": total_liab,
                "interest_debt": interest_debt,
                "cash_to_liab": cash_like / total_liab if total_liab else np.nan,
                "cash_to_interest_debt": cash_like / interest_debt if interest_debt else np.inf,
                "cfo_to_profit": cfo / profit if profit else np.nan,
                "payout_cash": dividend_paid / profit if profit else np.nan,
            }
        )
    return pd.DataFrame(rows)


def verdict(row: pd.Series) -> tuple[str, str]:
    code = row["ts_code"]
    if code == "02156.HK":
        return (
            "可以进候选，但只放在 B+/复核层，不放核心前排",
            "严格净现金、盈利与现金流质量较好，股东回报不低；但地产链属性和关联/交易公告频繁，治理层给 Amber，需要逐条核对关联方质量。",
        )
    return (
        "可以跟踪，但不符合严格净现金主线",
        "估值和股东回报有吸引力，且近期有密集回购；但现金类资产不足以覆盖总负债，净现金/市值为负，治理层 Amber，更多是高息/低估值观察票而非管我财式净现金票。",
    )


def pick(df: pd.DataFrame, code: str) -> pd.Series:
    hit = df.loc[df["ts_code"].eq(code)] if "ts_code" in df.columns else pd.DataFrame()
    return hit.iloc[0] if not hit.empty else pd.Series(dtype=object)


def value(row: pd.Series, *names: str, default=np.nan):
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return row[name]
    return default


def one_based_rank(df: pd.DataFrame, code: str):
    if "ts_code" not in df.columns:
        return np.nan
    idx = df.index[df["ts_code"].eq(code)]
    return int(idx[0]) + 1 if len(idx) else np.nan


def main() -> None:
    ranked = pd.read_csv(OUT / "hk_ranked_candidates.csv")
    quality = pd.read_csv(OUT / "expanded_quality_candidates.csv")
    gov = pd.read_csv(OUT / "governance_filtered_candidates.csv")
    annual = pd.concat([annual_trend(c) for c in CODES], ignore_index=True)
    annual.to_csv(OUT / "property_service_target_annuals.csv", index=False)

    rows = []
    for code in CODES:
        r = pick(ranked, code)
        q = pick(quality, code)
        g = pick(gov, code)
        base = r if not r.empty else q
        if base.empty:
            base = g if not g.empty else pd.Series({"ts_code": code, "name": code})
        v, reason = verdict(r if not r.empty else pd.Series({"ts_code": code}))
        pool_status = "当前主榜/质量池命中" if not r.empty or not q.empty else "当前未入主榜或质量池，仅保留专项历史复核"
        rows.append(
            {
                "ts_code": code,
                "name": value(base, "name", default=code),
                "verdict": v,
                "reason": reason,
                "pool_status": pool_status,
                "quote_time": value(r, "quote_time", default=""),
                "price_hkd": value(r, "price_hkd"),
                "market_cap_hkd": value(r, "total_mv_hkd", "market_cap_hkd"),
                "turnover_hkd": value(r, "turnover_hkd"),
                "pb": value(r, "pb"),
                "pe_ttm": value(r, "pe_ttm"),
                "cash_to_liab": value(r, "cash_to_liab", default=value(q, "cash_to_liab")),
                "net_cash_to_mv": value(r, "net_cash_to_mv", default=value(q, "net_cash_to_mv")),
                "shareholder_return_yield": value(r, "dividend_paid_yield_est", "shareholder_return_yield", default=value(q, "shareholder_return_yield")),
                "profit_latest": value(r, "profit_latest"),
                "profit_positive_years": value(r, "profit_positive_years"),
                "cfo_to_profit_latest": value(r, "cfo_to_profit_latest", default=value(q, "cfo_profit_avg4")),
                "oneoff_ratio_latest": value(r, "oneoff_ratio_latest", default=value(q, "oneoff_latest")),
                "screen_score": value(r, "score", default=value(q, "screen_score")),
                "primary_rank": one_based_rank(ranked, code),
                "quality_tier": value(q, "tier", default="未入当前质量池"),
                "quality_score": value(q, "quality_score"),
                "quality_rank": one_based_rank(quality, code),
                "hard_flags": value(q, "hard_flags", default=""),
                "governance_grade": value(g, "governance_grade", default="未入当前治理过滤池"),
                "governance_score": value(g, "governance_score"),
                "governance_notes": value(g, "governance_notes", default=""),
                "final_score_after_governance": value(g, "final_score_after_governance"),
                "governance_rank": one_based_rank(gov, code),
            }
        )
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "property_service_target_review.csv", index=False)

    md = ["# 永升服务、建发物业专项复核", "", "行情口径：腾讯港股 2026-06-25 约 15:01；财务口径：Tushare 港股报表缓存，最新年报 2025-12-31。", ""]
    for _, r in summary.iterrows():
        md += [
            f"## {r['ts_code']} {r['name']}",
            "",
            f"- 结论：{r['verdict']}",
            f"- 理由：{r['reason']}",
            f"- 价格/市值/成交额：{r['price_hkd']:.3f} HKD / {yi(r['market_cap_hkd'])} 亿 / {yi(r['turnover_hkd'])} 亿",
            f"- PB/PE：{r['pb']:.2f} / {r['pe_ttm']:.2f}",
            f"- 现金/负债、净现金/市值：{pct(r['cash_to_liab'])} / {pct(r['net_cash_to_mv'])}",
            f"- 股东回报率、最新利润、CFO/利润：{pct(r['shareholder_return_yield'])} / {yi(r['profit_latest'])} 亿 / {pct(r['cfo_to_profit_latest'])}",
            f"- 排名：主榜 {r['primary_rank']}，质量池 {r['quality_rank']}（{r['quality_tier']}），治理后 {r['governance_rank']}（{r['governance_grade']}）",
            f"- 风险：{r['hard_flags']}；{r['governance_notes']}",
            "",
        ]
        trend = annual.loc[annual["ts_code"].eq(r["ts_code"])].copy()
        trend_show = trend[["end_date", "revenue", "profit_owner", "cfo", "dividend_paid", "buyback", "cash_to_liab", "cfo_to_profit", "payout_cash", "oneoff_to_profit"]].copy()
        for col in ["revenue", "profit_owner", "cfo", "dividend_paid", "buyback"]:
            trend_show[col] = trend_show[col].map(yi)
        for col in ["cash_to_liab", "cfo_to_profit", "payout_cash", "oneoff_to_profit"]:
            trend_show[col] = trend_show[col].map(pct)
        md += [df_to_markdown(trend_show, index=False), ""]
    (OUT / "property_service_target_review.md").write_text("\n".join(md), encoding="utf-8")

    cards = []
    for _, r in summary.iterrows():
        cards.append(
            f"""
<article class="card">
  <h2>{esc(r['ts_code'])} {esc(r['name'])}</h2>
  <p><b>{esc(r['verdict'])}</b></p>
  <p>{esc(r['pool_status'])}</p>
  <p>{esc(r['reason'])}</p>
  <div class="grid">
    <div><label>价格/时间</label><strong>{float(r['price_hkd']):.3f} · {esc(r['quote_time'])}</strong></div>
    <div><label>市值/成交额</label><strong>{yi(r['market_cap_hkd'])}亿 / {yi(r['turnover_hkd'])}亿</strong></div>
    <div><label>PB/PE</label><strong>{float(r['pb']):.2f} / {float(r['pe_ttm']):.2f}</strong></div>
    <div><label>现金/负债</label><strong>{pct(r['cash_to_liab'])}</strong></div>
    <div><label>净现金/市值</label><strong>{pct(r['net_cash_to_mv'])}</strong></div>
    <div><label>股东回报率</label><strong>{pct(r['shareholder_return_yield'])}</strong></div>
    <div><label>质量/治理</label><strong>{esc(r['quality_tier'])} / {esc(r['governance_grade'])}</strong></div>
    <div><label>治理后排名</label><strong>{rank_text(r['governance_rank'])}</strong></div>
  </div>
  <p class="risk">{esc(r['hard_flags'])}；{esc(r['governance_notes'])}</p>
</article>"""
        )
    annual_show = annual.copy()
    for col in ["revenue", "profit_owner", "cfo", "dividend_paid", "buyback", "cash_like", "total_liabilities"]:
        annual_show[col] = annual_show[col].map(yi)
    for col in ["cash_to_liab", "cash_to_interest_debt", "cfo_to_profit", "payout_cash", "oneoff_to_profit"]:
        annual_show[col] = annual_show[col].map(pct)
    table = annual_show[["ts_code", "end_date", "revenue", "profit_owner", "cfo", "dividend_paid", "buyback", "cash_like", "total_liabilities", "cash_to_liab", "cfo_to_profit", "payout_cash", "oneoff_to_profit"]].to_html(index=False, escape=True)
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>物业股专项复核</title><style>
body{{margin:0;background:#f7f8fa;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',Arial,sans-serif}}header{{position:sticky;top:0;background:rgba(247,248,250,.94);border-bottom:1px solid #dbe1e8;padding:12px 14px}}main{{max-width:1120px;margin:0 auto;padding:14px}}h1{{font-size:20px;margin:0 0 4px}}.sub,p{{font-size:13px;line-height:1.5;color:#3d4a5c}}.links{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 16px}}.links a{{background:#176b87;color:white;text-decoration:none;padding:8px 10px;border-radius:6px;font-size:14px}}.card{{background:white;border:1px solid #dbe1e8;border-radius:8px;padding:12px;margin:10px 0}}.card h2{{font-size:18px;margin:0 0 8px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}}.grid div{{background:#f3f6f8;border-radius:6px;padding:8px}}label{{display:block;color:#637083;font-size:12px;margin-bottom:3px}}strong{{font-size:14px}}.risk{{color:#8a4b11}}.tablewrap{{overflow:auto;background:white;border:1px solid #dbe1e8;border-radius:8px;margin-top:14px}}table{{border-collapse:collapse;min-width:980px;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #e4e9ef;padding:8px;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}@media(min-width:780px){{.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.grid{{grid-template-columns:repeat(4,minmax(0,1fr))}}}}</style></head>
<body><header><h1>物业股专项复核</h1><div class="sub">永升服务、建发物业是否适合作为净现金/股东回报候选。行情：腾讯港股 2026-06-25 约 15:01；财务：Tushare 最新年报 2025-12-31。</div></header><main><div class="links"><a href="index.html">主榜</a><a href="governance_risk_overlay.html">治理过滤</a><a href="property_service_target_review.csv">CSV</a><a href="property_service_target_review.md">Markdown</a></div><section class="cards">{''.join(cards)}</section><h2>四年趋势</h2><div class="tablewrap">{table}</div></main></body></html>"""
    (OUT / "property_service_target_review.html").write_text(doc, encoding="utf-8")
    if PUBLIC.exists():
        for name in [
            "property_service_target_review.csv",
            "property_service_target_review.md",
            "property_service_target_review.html",
            "property_service_target_annuals.csv",
        ]:
            (PUBLIC / name).write_bytes((OUT / name).read_bytes())

    print(OUT / "property_service_target_review.csv")
    print(OUT / "property_service_target_review.html")


if __name__ == "__main__":
    main()
