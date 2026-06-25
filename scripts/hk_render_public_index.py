#!/usr/bin/env python3
"""Render the static public index page from the existing HK screen CSVs."""

from __future__ import annotations

import html
import math
import os
import re
from pathlib import Path

import pandas as pd

from hk_markdown import df_to_markdown


ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", ROOT)).resolve()
PUBLIC = Path(os.environ.get("HK_NETCASH_PUBLIC_DIR", ROOT)).resolve()
DISPLAY_LIMIT = 40


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


def fmt_float(value, digits: int = 2) -> str:
    if is_missing(value):
        return ""
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.{digits}f}"


def yi(value) -> str:
    if is_missing(value):
        return ""
    return f"{float(value) / 1e8:.2f}亿"


def pct(value) -> str:
    if is_missing(value):
        return ""
    return f"{float(value) * 100:.1f}%"


def compact_text(value) -> str:
    if is_missing(value):
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clip_text(value: str, max_len: int = 180) -> str:
    text = compact_text(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


GENERIC_CHINESE_NOTICE = (
    "An announcement has just been published by the issuer in the Chinese section of this website, "
    "a corresponding version of which may or may not be published in this section"
)


def split_latest_titles(value) -> list[str]:
    text = compact_text(value)
    if not text:
        return []
    parts = [compact_text(x) for x in text.split(" || ")]
    out: list[str] = []
    generic_count = 0
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        if GENERIC_CHINESE_NOTICE.lower() in part.lower():
            generic_count += 1
            continue
        if part in seen:
            continue
        seen.add(part)
        out.append(clip_text(part))
    if generic_count:
        out.insert(0, f"中文区公告 {generic_count} 条，英文页未同步标题")
    return out[:4]


def load_recent_titles() -> dict[str, list[str]]:
    title_path = OUT / "top20_hkex_announcement_titles.csv"
    titles: dict[str, list[str]] = {}
    if title_path.exists():
        df = pd.read_csv(title_path)
        for _, row in df.iterrows():
            titles[str(row["ts_code"])] = split_latest_titles(row.get("latest_titles"))

    full_path = OUT / "hkex_candidate_titles_2021_2026.csv"
    if full_path.exists():
        usecols = ["ts_code", "date_time", "title"]
        full = pd.read_csv(full_path, usecols=usecols)
        for code, group in full.groupby("ts_code", sort=False):
            if code in titles and titles[code]:
                continue
            rows = []
            generic_count = 0
            seen: set[str] = set()
            for _, r in group.head(8).iterrows():
                title = compact_text(r["title"])
                if not title:
                    continue
                if GENERIC_CHINESE_NOTICE.lower() in title.lower():
                    generic_count += 1
                    continue
                line = compact_text(f"{r['date_time']}: {title}")
                if line in seen:
                    continue
                seen.add(line)
                rows.append(clip_text(line))
            if generic_count:
                rows.insert(0, f"中文区公告 {generic_count} 条，英文页未同步标题")
            titles[code] = rows[:4]
    return titles


def tag_span(text: str, danger: bool = False) -> str:
    cls = "tag danger" if danger else "tag"
    return f'<span class="{cls}">{esc(text)}</span>'


def render_card(rank: int, row: pd.Series, recent_titles: dict[str, list[str]]) -> str:
    code = str(row["ts_code"])
    flags = [x for x in str(row.get("flags") or "").split(";") if x and x != "nan"]
    tags = []
    for flag in flags:
        danger = ("一次性" in flag and "高" in flag) or "Profit warning" in flag or "Inside information" in flag
        tags.append(tag_span(flag, danger=danger))
    if (row.get("strict_net_cash") is True or str(row.get("strict_net_cash")).lower() == "true") and "现金类资产>总负债" not in flags:
        tags.insert(0, tag_span("现金类资产>总负债"))

    notices = recent_titles.get(code, [])
    notice_html = ""
    if notices:
        items = "".join(f"<li>{esc(x)}</li>" for x in notices)
        notice_html = f"""
          <div class="notice-title">近期披露易标题</div>
          <ul class="notices">{items}</ul>"""
    else:
        notice_html = '<p class="muted">未抓取到近期披露易标题；可在 CSV 中继续人工复核。</p>'

    return f"""
    <article class="card">
      <div class="card-head">
        <div><span class="rank">#{rank}</span> <b>{esc(code)}</b> <span>{esc(row['name'])}</span></div>
        <div class="score">{fmt_float(row['score'], 1)}</div>
      </div>
      <div class="grid">
        <div><label>市值</label><strong>{yi(row['total_mv_hkd'])}</strong></div>
        <div><label>成交额</label><strong>{yi(row['turnover_hkd'])}</strong></div>
        <div><label>PB/PE</label><strong>{fmt_float(row['pb'])} / {fmt_float(row['pe_ttm'])}</strong></div>
        <div><label>现金/负债</label><strong>{pct(row['cash_to_liab'])}</strong></div>
        <div><label>净现金/市值</label><strong>{pct(row['net_cash_to_mv'])}</strong></div>
        <div><label>股东回报率</label><strong>{pct(row['dividend_paid_yield_est'])}</strong></div>
        <div><label>近4年盈利</label><strong>{int(row['profit_positive_years'])}/4</strong></div>
        <div><label>CFO/利润</label><strong>{pct(row['cfo_to_profit_latest'])}</strong></div>
      </div>
      <div class="tags">{''.join(tags)}</div>
      <details class="detail">
        <summary><span class="closed-label">展开详情</span><span class="open-label">收起详情</span></summary>
        <div class="detail-body">
          <div class="detail-grid">
            <div><label>最新报告期</label><strong>{esc(row['latest_report_date'])}</strong></div>
            <div><label>最近一年利润</label><strong>{yi(row['profit_latest'])}</strong></div>
            <div><label>一次性/其他收益占比</label><strong>{pct(row['oneoff_ratio_latest'])}</strong></div>
            <div><label>最近派息/利润</label><strong>{pct(float(row['div_paid_latest']) / float(row['profit_latest']) if float(row['profit_latest'] or 0) else float('nan'))}</strong></div>
            <div><label>DPS估算收益率</label><strong>{pct(row['dps_yield_est'])}</strong></div>
            <div><label>流动性</label><strong>{esc(row['liquidity_bucket'])}</strong></div>
          </div>
          {notice_html}
        </div>
      </details>
    </article>
    """


def render_index() -> str:
    ranked = pd.read_csv(OUT / "hk_ranked_candidates.csv")
    recent_titles = load_recent_titles()
    quote_times = ranked.get("quote_time", pd.Series(dtype=str)).dropna().astype(str)
    quote_meta = f"行情：腾讯港股 {quote_times.max()}" if len(quote_times) else "行情：最新可得港股报价"
    show = ranked.head(DISPLAY_LIMIT)
    cards = [render_card(i + 1, row, recent_titles) for i, (_, row) in enumerate(show.iterrows())]
    table_cols = [
        "ts_code",
        "name",
        "total_mv_hkd",
        "turnover_hkd",
        "pb",
        "pe_ttm",
        "cash_to_liab",
        "net_cash_to_mv",
        "profit_latest",
        "profit_positive_years",
        "cfo_to_profit_latest",
        "oneoff_ratio_latest",
        "dividend_paid_yield_est",
        "score",
        "liquidity_bucket",
        "flags",
    ]
    table = ranked.head(80)[table_cols].copy()
    for col in ["total_mv_hkd", "turnover_hkd", "profit_latest"]:
        table[col] = table[col].map(yi)
    for col in ["pb", "pe_ttm"]:
        table[col] = table[col].map(lambda x: fmt_float(x, 3))
    for col in ["cash_to_liab", "net_cash_to_mv", "cfo_to_profit_latest", "oneoff_ratio_latest", "dividend_paid_yield_est"]:
        table[col] = table[col].map(pct)
    table["score"] = table["score"].map(lambda x: fmt_float(x, 1))
    table.rename(
        columns={
            "ts_code": "代码",
            "name": "名称",
            "total_mv_hkd": "市值",
            "turnover_hkd": "成交额",
            "pb": "PB",
            "pe_ttm": "PE",
            "cash_to_liab": "现金/负债",
            "net_cash_to_mv": "净现金/市值",
            "profit_latest": "利润",
            "profit_positive_years": "盈利年数",
            "cfo_to_profit_latest": "CFO/利润",
            "oneoff_ratio_latest": "一次性占比",
            "dividend_paid_yield_est": "股东回报率",
            "score": "分数",
            "liquidity_bucket": "流动性",
            "flags": "标签",
        },
        inplace=True,
    )
    table_html = table.to_html(index=False, escape=True, classes="rank-table")
    md = [
        "# 港股净现金/股东回报筛选结果",
        "",
        f"- {quote_meta}",
        f"- 主榜通过财务硬门槛数量: {len(ranked)}",
        "- 财务口径: Tushare 港股报表缓存，行情口径: 腾讯港股最新报价；PB/PE 沿用原行情口径并按价格变化微调。",
        "",
        df_to_markdown(table, index=False),
        "",
    ]
    (OUT / "hk_ranked_candidates.md").write_text("\n".join(md), encoding="utf-8")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>港股净现金候选列表</title>
<style>
:root {{ color-scheme: light; --bg:#f7f8fa; --panel:#fff; --text:#17202a; --muted:#637083; --line:#dbe1e8; --accent:#176b87; --danger:#a73838; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Noto Sans CJK SC",Arial,sans-serif; background:var(--bg); color:var(--text); }}
header {{ position:sticky; top:0; z-index:2; background:rgba(247,248,250,.94); backdrop-filter:blur(10px); border-bottom:1px solid var(--line); padding:12px 14px; }}
h1 {{ margin:0 0 4px; font-size:20px; }}
.meta {{ color:var(--muted); font-size:13px; line-height:1.45; }}
main {{ max-width:1100px; margin:0 auto; padding:14px; }}
.links {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0 16px; }}
a.button {{ text-decoration:none; color:#fff; background:var(--accent); padding:8px 10px; border-radius:6px; font-size:14px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; margin:10px 0; box-shadow:0 1px 2px rgba(20,30,40,.04); }}
.card-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:16px; }}
.rank {{ color:var(--muted); margin-right:4px; }}
.score {{ min-width:48px; text-align:right; font-weight:700; color:var(--accent); }}
.grid,.detail-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:10px; }}
.grid div,.detail-grid div {{ background:#f3f6f8; border-radius:6px; padding:8px; min-width:0; }}
label {{ display:block; color:var(--muted); font-size:12px; margin-bottom:3px; }}
strong {{ font-size:14px; overflow-wrap:anywhere; }}
.tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }}
.tag {{ border:1px solid #c8d7df; color:#315463; background:#eef6f8; border-radius:999px; padding:3px 7px; font-size:12px; }}
.tag.danger {{ border-color:#e0b6b6; color:var(--danger); background:#fff1f1; }}
.detail {{ margin-top:10px; border-top:1px solid var(--line); padding-top:8px; }}
.detail summary {{ cursor:pointer; width:max-content; max-width:100%; color:var(--accent); font-size:13px; font-weight:700; list-style:none; user-select:none; }}
.detail summary::-webkit-details-marker {{ display:none; }}
.detail summary::before {{ content:"+"; display:inline-block; width:16px; color:var(--accent); }}
.detail[open] summary::before {{ content:"-"; }}
.detail .open-label {{ display:none; }}
.detail[open] .open-label {{ display:inline; }}
.detail[open] .closed-label {{ display:none; }}
.detail-body {{ margin-top:8px; background:#fbfcfd; border:1px solid #e5ebf0; border-radius:8px; padding:10px; }}
.notice-title {{ color:var(--muted); font-size:12px; margin:10px 0 4px; }}
.notices {{ margin:0; padding-left:18px; color:#3d4a5c; font-size:13px; line-height:1.45; }}
.notices li {{ margin:5px 0; overflow-wrap:anywhere; }}
.muted {{ color:var(--muted); font-size:13px; line-height:1.45; margin:8px 0 0; }}
.table-wrap {{ overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px; margin-top:18px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; min-width:860px; }}
th,td {{ padding:8px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }}
th:nth-child(1),th:nth-child(2),td:nth-child(1),td:nth-child(2),th:last-child,td:last-child {{ text-align:left; }}
th:last-child,td:last-child {{ white-space:normal; min-width:220px; }}
th {{ position:sticky; top:0; background:#eef2f5; }}
section h2 {{ font-size:17px; margin:22px 0 8px; }}
@media (min-width:760px) {{ .grid {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} .detail-grid {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} .cards {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }} .card {{ margin:0; }} }}
</style>
</head>
<body>
<header>
  <h1>港股净现金/股东回报候选</h1>
  <div class="meta">Tushare 财务为主，腾讯港股行情更新价格/成交额/市值，披露易标题做风险提示。{esc(quote_meta)}。手机横向滚动可看完整表。</div>
</header>
<main>
  <div class="links">
    <a class="button" href="secondary_validation_top20.html">前20二次检验</a>
    <a class="button" href="property_service_target_review.html">物业股专项复核</a>
    <a class="button" href="expanded_quality_candidates.html">宽口径合并榜</a>
    <a class="button" href="governance_risk_overlay.html">治理风险过滤</a>
    <a class="button" href="llm_digest.html">流水线汇总</a>
    <a class="button" href="governance_filtered_candidates.csv">治理过滤CSV</a>
    <a class="button" href="hk_ranked_candidates.csv">主榜CSV</a>
    <a class="button" href="hk_ranked_candidates.md">Markdown</a>
    <a class="button" href="top25_tushare_dividend_trends.csv">分红趋势</a>
  </div>
  <section>
    <h2>前 40 候选卡片</h2>
    <div class="cards">{''.join(cards)}</div>
  </section>
  <section>
    <h2>前 80 表格</h2>
    <div class="table-wrap">{table_html}</div>
  </section>
</main>
</body>
</html>
"""


def main() -> None:
    html_text = render_index()
    (OUT / "index.html").write_text(html_text, encoding="utf-8")
    if PUBLIC.exists():
        (PUBLIC / "index.html").write_text(html_text, encoding="utf-8")
    print(OUT / "index.html")
    if PUBLIC.exists():
        print(PUBLIC / "index.html")


if __name__ == "__main__":
    main()
