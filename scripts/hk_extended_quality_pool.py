#!/usr/bin/env python3
"""
Build a broader quality-oriented candidate pool from the expanded HK screen.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from hk_markdown import df_to_markdown


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve()
CACHE = OUT / "financial_cache"
DISPLAY_LIMIT = 180


INCOME = {
    "revenue": ["营业额", "经营收入", "经营收入总额", "营运收入"],
    "profit": ["股东应占溢利"],
    "oneoff": ["出售资产之溢利", "重估盈余", "按公平价值列账的金融资产公平值增加/减少", "其他收益", "其他收入"],
}
CF = {"cfo": ["经营业务现金净额"], "div": ["已付股息(融资)"], "buyback": ["回购股份"]}


def first(row, names):
    for n in names:
        if n in row.index and pd.notna(row[n]):
            return float(row[n])
    return np.nan


def sumv(row, names):
    vals = [float(row[n]) for n in names if n in row.index and pd.notna(row[n])]
    return float(np.nansum(vals)) if vals else np.nan


def load(code, ep):
    p = CACHE / f"{code.replace('.', '_')}.json"
    if not p.exists():
        return pd.DataFrame()
    obj = json.loads(p.read_text())
    df = pd.DataFrame(obj.get(ep, {}).get("data", []))
    if df.empty:
        return df
    df = df.set_index("end_date")
    df.index = df.index.astype(str)
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index()


def annual(df):
    if df.empty:
        return df
    dec = df[df.index.str.endswith("1231")]
    return dec if len(dec) >= 2 else df.groupby(df.index.str[:4]).tail(1)


def cagr(s):
    s = s.dropna()
    if len(s) < 2:
        return np.nan
    a, b = float(s.iloc[0]), float(s.iloc[-1])
    n = len(s) - 1
    if a <= 0 or b <= 0:
        return np.nan
    return (b / a) ** (1 / n) - 1


def trend(s):
    s = s.dropna()
    if len(s) < 3:
        return "样本不足"
    last, prev, high = float(s.iloc[-1]), float(s.iloc[-2]), float(s.max())
    if last <= 0:
        return "亏损"
    if prev > 0 and last > prev * 1.15:
        return "同比增长"
    if prev > 0 and last < prev * 0.85:
        return "同比下滑"
    if high > 0 and last < high * 0.70:
        return "高点回落"
    return "稳定"


def pct(x):
    if pd.isna(x) or math.isinf(float(x)):
        return ""
    return f"{float(x) * 100:.1f}%"


def yi(x):
    if pd.isna(x):
        return ""
    return f"{float(x) / 1e8:.2f}"


def industry_tags(name):
    tags = []
    if any(k in name for k in ["银行", "保险", "信贷", "金融", "证券", "租赁"]):
        tags.append("金融口径")
    if any(k in name for k in ["物业", "地产", "置业", "生活服务", "城市服务", "彩生活", "新希望服务", "星盛商业", "宝龙商业", "绿城服务", "雅生活", "商企服务"]):
        tags.append("地产链")
    if any(k in name for k in ["教育", "新东方", "东方教育", "粉笔", "中教"]):
        tags.append("教育")
    if any(k in name for k in ["医药", "医疗", "药业", "同仁堂", "丽珠", "康哲", "国药"]):
        tags.append("医药")
    if any(k in name for k in ["食品", "饮料", "白花油", "茶", "飞鹤", "六福", "周黑鸭", "大家乐", "日清", "谭木匠", "利郎", "味千", "卫龙", "蜜雪", "李宁", "旺旺", "康师傅", "百胜", "普拉达", "优品360", "小菜园", "珍酒", "九毛九"]):
        tags.append("消费")
    if any(k in name for k in ["高速", "港口", "燃气", "电力", "水务", "环保", "冠德", "民航信息", "铁路", "航运", "煤气", "电能", "水泥"]):
        tags.append("公用/基建")
    return tags


def main():
    source = OUT / "hk_loose_ranked_candidates.csv"
    if not source.exists():
        source = OUT / "hk_ranked_candidates.csv"
    base = pd.read_csv(source)
    base["source_batch"] = base.get("source_batch", "原始初筛").fillna("原始初筛")
    primary_code = base["ts_code"].astype(str).str.extract(r"^(\d{5})\.HK$", expand=False)
    base = base.loc[~primary_code.fillna("").str.startswith("8")].copy()
    rows = []
    for _, r in base.iterrows():
        code = r["ts_code"]
        inc = annual(load(code, "hk_income")).tail(4)
        cf = annual(load(code, "hk_cashflow")).tail(4)
        years = sorted(set(inc.index).intersection(set(cf.index)))
        if not years:
            continue
        inc = inc.loc[years]
        cf = cf.loc[years]
        revenue = inc.apply(lambda x: first(x, INCOME["revenue"]), axis=1)
        profit = inc.apply(lambda x: first(x, INCOME["profit"]), axis=1)
        cfo = cf.apply(lambda x: first(x, CF["cfo"]), axis=1)
        div = cf.apply(lambda x: abs(first(x, CF["div"])), axis=1)
        buyback = cf.apply(lambda x: abs(first(x, CF["buyback"])), axis=1)
        oneoff = inc.apply(lambda x: sumv(x, INCOME["oneoff"]), axis=1)
        latest_profit = profit.iloc[-1]
        cfo_profit = (cfo / profit.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
        payout = (div / profit.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
        oneoff_ratio = oneoff.iloc[-1] / abs(latest_profit) if pd.notna(latest_profit) and latest_profit else np.nan
        tags = industry_tags(str(r["name"]))

        q = 0.0
        q += min(max(float(r["cash_to_liab"]), 0), 3) * 8
        q += min(max(float(r["net_cash_to_mv"]), -1), 1.5) * 10
        q += int((profit > 0).sum()) * 5
        q += int((cfo > 0).sum()) * 5
        q += min(max(cfo_profit.mean() if pd.notna(cfo_profit.mean()) else 0, 0), 2) * 10
        q += min(max(float(r["dividend_paid_yield_est"]) if pd.notna(r["dividend_paid_yield_est"]) else 0, 0), 0.12) * 220
        q += int((div.fillna(0) > 0).sum()) * 3
        if trend(revenue) in {"稳定", "同比增长"}:
            q += 8
        else:
            q -= 8
        if trend(profit) in {"稳定", "同比增长"}:
            q += 10
        else:
            q -= 10
        if pd.notna(oneoff_ratio):
            q -= min(max(oneoff_ratio, 0), 1.5) * 22
        if payout.iloc[-1] > 1.2:
            q -= 10
        if float(r["turnover_hkd"]) < 100000:
            q -= 4
        if "金融口径" in tags:
            q -= 16
        if "地产链" in tags:
            q -= 12
        if float(r["cash_to_liab"]) < 1:
            q -= 10

        hard_flags = []
        if oneoff_ratio > 0.5:
            hard_flags.append("一次性占比高")
        elif oneoff_ratio > 0.35:
            hard_flags.append("一次性偏高")
        if payout.iloc[-1] > 1.2:
            hard_flags.append("派息透支")
        elif payout.iloc[-1] > 1.0:
            hard_flags.append("派息偏激进")
        if float(r["cash_to_liab"]) < 1:
            hard_flags.append("非严格净现金")
        if cfo_profit.mean() < 0.7:
            hard_flags.append("现金流覆盖弱")
        if trend(profit) not in {"稳定", "同比增长"}:
            hard_flags.append("利润趋势弱")
        if "金融口径" in tags:
            hard_flags.append("金融口径另看")
        if "地产链" in tags:
            hard_flags.append("地产链折价")
        if int((div.fillna(0) > 0).sum()) < 3 and float(r["dividend_paid_yield_est"]) > 0.04:
            hard_flags.append("回购多于现金分红")

        if q >= 95 and len(hard_flags) == 0 and float(r["cash_to_liab"]) >= 1:
            tier = "A"
        elif q >= 80 and len(hard_flags) <= 1:
            tier = "B+"
        elif q >= 65 and len(hard_flags) <= 2:
            tier = "B"
        else:
            tier = "Watch"

        rows.append({
            "tier": tier,
            "quality_score": q,
            "ts_code": code,
            "name": r["name"],
            "source_batch": r["source_batch"],
            "tags": ",".join(tags),
            "market_cap_hkd": r["total_mv_hkd"],
            "turnover_hkd": r["turnover_hkd"],
            "pb": r["pb"],
            "pe_ttm": r["pe_ttm"],
            "cash_to_liab": r["cash_to_liab"],
            "net_cash_to_mv": r["net_cash_to_mv"],
            "shareholder_return_yield": r["dividend_paid_yield_est"],
            "div_paid_years": int((div.fillna(0) > 0).sum()),
            "latest_payout": payout.iloc[-1],
            "profit_trend": trend(profit),
            "revenue_trend": trend(revenue),
            "cfo_profit_avg4": cfo_profit.mean(),
            "oneoff_latest": oneoff_ratio,
            "screen_score": r["score"],
            "hard_flags": "；".join(hard_flags) or "无明显硬伤",
        })

    out = pd.DataFrame(rows)
    tier_order = {"A": 0, "B+": 1, "B": 2, "Watch": 3}
    out["tier_order"] = out["tier"].map(tier_order).fillna(9)
    out = out.sort_values(["tier_order", "quality_score"], ascending=[True, False]).drop(columns=["tier_order"])
    out.to_csv(OUT / "expanded_quality_candidates.csv", index=False)

    show = out.copy()
    for c in ["market_cap_hkd", "turnover_hkd"]:
        show[c] = show[c].map(yi)
    for c in ["cash_to_liab", "net_cash_to_mv", "shareholder_return_yield", "latest_payout", "cfo_profit_avg4", "oneoff_latest"]:
        show[c] = show[c].map(pct)
    show["quality_score"] = show["quality_score"].round(1)
    show["screen_score"] = show["screen_score"].round(1)
    cols = [
        "tier", "ts_code", "name", "source_batch", "tags", "quality_score", "market_cap_hkd", "pb", "pe_ttm",
        "cash_to_liab", "net_cash_to_mv", "shareholder_return_yield", "div_paid_years",
        "latest_payout", "profit_trend", "revenue_trend", "cfo_profit_avg4", "oneoff_latest", "hard_flags"
    ]
    md = [
        "# 扩展质量候选池",
        "",
        f"这版从 `{source.name}` 重排，不再单纯追求机械净现金分。A/B+ 更适合继续做人工研究；Watch 只作线索。",
        f"完整候选 {len(out)} 个，页面展示前 {min(DISPLAY_LIMIT, len(out))} 个。",
        "",
        df_to_markdown(show.head(DISPLAY_LIMIT)[cols], index=False),
        "",
    ]
    (OUT / "expanded_quality_candidates.md").write_text("\\n".join(md), encoding="utf-8")

    cards = []
    for _, r in show.head(DISPLAY_LIMIT).iterrows():
        cls = "good" if r["tier"] == "A" else ("mid" if r["tier"] in ("B+", "B") else "watch")
        cards.append(f"""
<article class="card {cls}">
  <div class="head"><b>{r['tier']}</b><span>{r['ts_code']} {r['name']}</span><em>{r['quality_score']}</em></div>
  <div class="meta">{r['source_batch']} · {r['tags'] or '未标注行业'} · {r['hard_flags']}</div>
  <div class="grid">
    <div><label>市值</label><strong>{r['market_cap_hkd']}亿</strong></div>
    <div><label>PB/PE</label><strong>{r['pb']:.2f}/{r['pe_ttm']:.2f}</strong></div>
    <div><label>现金/负债</label><strong>{r['cash_to_liab']}</strong></div>
    <div><label>净现金/市值</label><strong>{r['net_cash_to_mv']}</strong></div>
    <div><label>股东回报</label><strong>{r['shareholder_return_yield']}</strong></div>
    <div><label>派息年数</label><strong>{r['div_paid_years']}/4</strong></div>
    <div><label>利润趋势</label><strong>{r['profit_trend']}</strong></div>
    <div><label>CFO/利润</label><strong>{r['cfo_profit_avg4']}</strong></div>
  </div>
</article>""")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>扩展质量候选池</title><style>
body{{margin:0;background:#f7f8fa;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',Arial,sans-serif}}header{{position:sticky;top:0;background:rgba(247,248,250,.94);backdrop-filter:blur(10px);border-bottom:1px solid #dbe1e8;padding:12px 14px}}h1{{font-size:20px;margin:0 0 4px}}.sub{{font-size:13px;color:#637083}}main{{max-width:1120px;margin:0 auto;padding:14px}}.links{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 16px}}.links a{{background:#176b87;color:white;text-decoration:none;padding:8px 10px;border-radius:6px;font-size:14px}}.card{{background:white;border:1px solid #dbe1e8;border-left-width:5px;border-radius:8px;padding:12px;margin:10px 0}}.good{{border-left-color:#247a4d}}.mid{{border-left-color:#b77a1a}}.watch{{border-left-color:#687386}}.head{{display:flex;gap:8px;align-items:center}}.head b{{font-size:20px;min-width:42px}}.head span{{font-weight:700;flex:1}}.head em{{font-style:normal;color:#176b87;font-weight:700}}.meta{{margin-top:6px;color:#637083;font-size:13px;line-height:1.4}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}}.grid div{{background:#f3f6f8;border-radius:6px;padding:8px}}label{{display:block;color:#637083;font-size:12px;margin-bottom:3px}}strong{{font-size:14px}}@media(min-width:760px){{.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card{{margin:0}}.grid{{grid-template-columns:repeat(4,minmax(0,1fr))}}}}</style></head>
<body><header><h1>扩展质量候选池</h1><div class="sub">宽口径合并 {len(out)} 个候选，展示前 {min(DISPLAY_LIMIT, len(out))} 个：稳定派息、现金流、低一次性收益优先；地产链/金融口径降权。</div></header><main><div class="links"><a href="index.html">主榜</a><a href="governance_risk_overlay.html">治理风险过滤</a><a href="expanded_quality_candidates.csv">质量CSV</a><a href="hk_supplemental_candidates.csv">新增CSV</a><a href="hk_loose_ranked_candidates.csv">宽筛硬门槛CSV</a><a href="expanded_quality_candidates.md">Markdown</a></div><section class="cards">{''.join(cards)}</section></main></body></html>"""
    (OUT / "expanded_quality_candidates.html").write_text(html, encoding="utf-8")
    print(OUT / "expanded_quality_candidates.csv")
    print(OUT / "expanded_quality_candidates.md")
    print(OUT / "expanded_quality_candidates.html")


if __name__ == "__main__":
    main()
