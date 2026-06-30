#!/usr/bin/env python3
"""Rank Shanghai/Shenzhen-listed dividend ETFs by live distribution yield."""

from __future__ import annotations

import html
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import tushare as ts

from hk_markdown import df_to_markdown


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve()
DAILY_CACHE_DIR = OUT / "a_etf_daily_cache"

DIVIDEND_PATTERNS = ("红利", "股息", "高息", "高股息", "分红")
EXCLUDE_PATTERNS = ("纳指", "纳斯达克", "日经", "德国", "东南亚", "美国", "全球")
SINA_URL = "https://hq.sinajs.cn/list={symbols}"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def sh_sz_symbol(ts_code: str) -> str:
    code, exchange = str(ts_code).split(".")
    return ("sh" if exchange == "SH" else "sz") + code


def parse_sina_line(line: str) -> dict | None:
    if '="' not in line:
        return None
    left, body = line.split('="', 1)
    symbol = left.rsplit("_", 1)[-1]
    fields = body.rstrip('";').split(",")
    if len(fields) < 32 or not fields[0]:
        return None
    exchange = "SH" if symbol.startswith("sh") else "SZ"
    code = symbol[2:]
    nums = [pd.to_numeric(fields[i], errors="coerce") for i in range(1, 10)]
    price = nums[2]
    if pd.isna(price) or float(price) <= 0:
        price = nums[1]
    return {
        "ts_code": f"{code}.{exchange}",
        "quote_name": fields[0],
        "open": nums[0],
        "pre_close": nums[1],
        "price": price,
        "high": nums[3],
        "low": nums[4],
        "volume_shares": nums[7],
        "amount_cny": nums[8],
        "quote_date": fields[30],
        "quote_time": fields[31],
        "quote_source": "sina_realtime",
    }


def fetch_sina_quotes(codes: list[str], batch_size: int = 70) -> pd.DataFrame:
    rows: list[dict] = []
    session = requests.Session()
    session.headers.update(UA)
    for i in range(0, len(codes), batch_size):
        symbols = ",".join(sh_sz_symbol(code) for code in codes[i : i + batch_size])
        resp = session.get(SINA_URL.format(symbols=symbols), timeout=20)
        resp.raise_for_status()
        resp.encoding = "gb18030"
        for line in resp.text.splitlines():
            parsed = parse_sina_line(line)
            if parsed:
                rows.append(parsed)
        time.sleep(0.08)
    return pd.DataFrame(rows).drop_duplicates("ts_code", keep="last") if rows else pd.DataFrame()


def norm_text(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def is_dividend_etf(row: pd.Series) -> bool:
    name = norm_text(row.get("name", ""))
    if "ETF" not in name or any(p in name for p in ("联接", "LOF", "混合")):
        return False
    text = name + norm_text(row.get("benchmark", ""))
    if not any(p in text for p in DIVIDEND_PATTERNS):
        return False
    if any(p in text for p in EXCLUDE_PATTERNS):
        return False
    return True


def strategy_tag(row: pd.Series) -> str:
    text = norm_text(row.get("name", "")) + norm_text(row.get("benchmark", ""))
    tags = []
    for key in [
        "上证红利",
        "深证红利",
        "中证红利",
        "红利低波",
        "央企红利",
        "国企红利",
        "高股息",
        "红利质量",
        "红利价值",
        "恒生高股息",
        "恒生红利",
        "港股通高股息",
        "港股红利",
        "H股红利",
    ]:
        if key in text:
            tags.append(key)
    if "A股" in text and "A股红利" not in tags:
        tags.append("A股红利")
    return " / ".join(dict.fromkeys(tags)) or "红利/股息"


def date_num(value) -> pd.Timestamp:
    text = str(value or "")
    if not text or text == "nan":
        return pd.NaT
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce")


def latest_nav(pro, ts_code: str, start_date: str, end_date: str) -> dict:
    try:
        nav = pro.fund_nav(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception:
        return {}
    if nav.empty:
        return {}
    nav = nav.sort_values("nav_date", ascending=False)
    row = nav.iloc[0]
    return {
        "nav_date": row.get("nav_date"),
        "unit_nav": pd.to_numeric(row.get("unit_nav"), errors="coerce"),
        "accum_nav": pd.to_numeric(row.get("accum_nav"), errors="coerce"),
        "net_asset": pd.to_numeric(row.get("net_asset"), errors="coerce"),
        "total_netasset": pd.to_numeric(row.get("total_netasset"), errors="coerce"),
    }


def fetch_dividend_events(pro, ts_code: str) -> pd.DataFrame:
    try:
        div = pro.fund_div(ts_code=ts_code)
    except Exception:
        return pd.DataFrame(columns=["event_date", "event_ts", "div_cash"])
    if div.empty:
        return pd.DataFrame(columns=["event_date", "event_ts", "div_cash"])
    div = div.copy()
    div["div_cash"] = pd.to_numeric(div["div_cash"], errors="coerce").fillna(0.0)
    div["event_date"] = div["ex_date"].where(div["ex_date"].notna(), div["pay_date"])
    div["event_date"] = div["event_date"].where(div["event_date"].notna(), div["ann_date"])
    div["event_ts"] = div["event_date"].map(date_num)
    div = div[(div["div_cash"] > 0) & div["event_ts"].notna()]
    div = div[~div["div_proc"].astype(str).str.contains("取消|不分配", na=False)]
    # Tushare fund_div may emit the same ETF cash distribution multiple times
    # with different base_unit values. Count a cash distribution event once.
    return div.drop_duplicates(subset=["event_date", "div_cash"], keep="first").sort_values("event_ts")


def dividend_stats_from_events(div: pd.DataFrame, asof: pd.Timestamp) -> dict:
    if div.empty:
        return {
            "div_cash_ttm": 0.0,
            "div_count_ttm": 0,
            "div_cash_3y": 0.0,
            "div_count_3y": 0,
            "div_years_3y": 0,
            "last_div_date": "",
    }
    ttm = div[div["event_ts"].between(asof - pd.Timedelta(days=365), asof)]
    three = div[div["event_ts"].between(asof - pd.Timedelta(days=365 * 3), asof)]
    last_date = div["event_ts"].max()
    return {
        "div_cash_ttm": float(ttm["div_cash"].sum()),
        "div_count_ttm": int(len(ttm)),
        "div_cash_3y": float(three["div_cash"].sum()),
        "div_count_3y": int(len(three)),
        "div_years_3y": int(three["event_ts"].dt.year.nunique()),
        "last_div_date": "" if pd.isna(last_date) else last_date.strftime("%Y%m%d"),
    }


def next_date_yyyymmdd(value: str) -> str:
    ts = pd.to_datetime(str(value), format="%Y%m%d", errors="coerce")
    if pd.isna(ts):
        return str(value)
    return (ts + pd.Timedelta(days=1)).strftime("%Y%m%d")


def fetch_fund_daily_history(pro, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DAILY_CACHE_DIR / f"{ts_code.replace('.', '_')}.csv"
    cached = pd.DataFrame()
    fetch_start = start_date
    if cache_path.exists():
        cached = pd.read_csv(cache_path, dtype={"trade_date": str})
        if not cached.empty and "trade_date" in cached.columns:
            cached["trade_date"] = cached["trade_date"].astype(str)
            latest = cached["trade_date"].max()
            fetch_start = next_date_yyyymmdd(latest)
    frames = [cached] if not cached.empty else []
    if fetch_start <= end_date:
        try:
            fresh = pro.fund_daily(ts_code=ts_code, start_date=fetch_start, end_date=end_date)
        except Exception:
            fresh = pd.DataFrame()
        if not fresh.empty:
            fresh["trade_date"] = fresh["trade_date"].astype(str)
            frames.append(fresh)
            time.sleep(0.03)
    if not frames:
        return pd.DataFrame()
    daily = pd.concat(frames, ignore_index=True)
    daily = daily.drop_duplicates("trade_date", keep="last").sort_values("trade_date")
    daily.to_csv(cache_path, index=False)
    return daily


def current_yield_percentile(values: pd.Series, current_yield: float) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty or pd.isna(current_yield) or current_yield <= 0:
        return np.nan
    return float((values <= current_yield).mean())


def yield_position_label(percentile: float, obs: int = 0) -> str:
    if pd.isna(percentile):
        return "暂无TTM分红"
    if obs < 252:
        return "样本较短"
    if percentile < 0.2:
        return "股息率历史低位/偏贵"
    if percentile < 0.4:
        return "股息率中低位"
    if percentile < 0.6:
        return "股息率中位"
    if percentile < 0.8:
        return "股息率中高位"
    return "股息率历史高位/偏便宜"


def historical_yield_stats(
    pro,
    ts_code: str,
    list_date: str,
    end_date: str,
    asof: pd.Timestamp,
    div_events: pd.DataFrame,
    current_price: float,
    current_div_cash_ttm: float,
    quote_date: str,
) -> dict:
    base = {
        "hist_yield_max": np.nan,
        "hist_yield_max_date": "",
        "hist_yield_obs": 0,
        "yield_pctile_all": np.nan,
        "yield_pctile_1y": np.nan,
        "yield_pctile_2y": np.nan,
        "yield_pctile_5y": np.nan,
        "yield_pctile_10y": np.nan,
        "yield_to_hist_max": np.nan,
        "yield_position_label": "暂无TTM分红",
    }
    start_date = str(list_date or "")
    if not re.fullmatch(r"\d{8}", start_date):
        start_date = "20000101"
    daily = fetch_fund_daily_history(pro, ts_code, start_date, end_date)
    if daily.empty:
        return base
    daily = daily.copy()
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    daily["trade_ts"] = pd.to_datetime(daily["trade_date"], format="%Y%m%d", errors="coerce")
    daily = daily[daily["trade_ts"].notna() & daily["close"].gt(0)].sort_values("trade_ts")
    if daily.empty or div_events.empty:
        return base

    events = div_events[["event_ts", "div_cash"]].dropna().sort_values("event_ts")
    event_days = events["event_ts"].map(lambda x: pd.Timestamp(x).toordinal()).to_numpy()
    event_cash = pd.to_numeric(events["div_cash"], errors="coerce").fillna(0).to_numpy()
    cumsum = np.r_[0.0, np.cumsum(event_cash)]
    ttm_cash = []
    for day in daily["trade_ts"].map(lambda x: pd.Timestamp(x).toordinal()).to_numpy():
        left = np.searchsorted(event_days, day - 365, side="left")
        right = np.searchsorted(event_days, day, side="right")
        ttm_cash.append(cumsum[right] - cumsum[left])
    daily["hist_div_cash_ttm"] = ttm_cash
    daily["hist_yield_ttm"] = daily["hist_div_cash_ttm"] / daily["close"].replace(0, np.nan)

    current_ts = pd.to_datetime(quote_date, errors="coerce")
    if pd.isna(current_ts):
        current_ts = asof
    if pd.notna(current_price) and float(current_price) > 0 and current_div_cash_ttm > 0:
        daily = daily[daily["trade_ts"] < current_ts].copy()
        daily = pd.concat(
            [
                daily,
                pd.DataFrame(
                    [
                        {
                            "trade_date": current_ts.strftime("%Y%m%d"),
                            "trade_ts": current_ts,
                            "close": float(current_price),
                            "hist_div_cash_ttm": float(current_div_cash_ttm),
                            "hist_yield_ttm": float(current_div_cash_ttm) / float(current_price),
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )

    valid = daily[daily["hist_div_cash_ttm"].gt(0) & daily["hist_yield_ttm"].notna()].copy()
    if valid.empty:
        return base
    max_idx = valid["hist_yield_ttm"].idxmax()
    current_yield = float(current_div_cash_ttm) / float(current_price) if pd.notna(current_price) and float(current_price) > 0 else np.nan
    out = dict(base)
    out["hist_yield_max"] = float(valid.loc[max_idx, "hist_yield_ttm"])
    out["hist_yield_max_date"] = str(valid.loc[max_idx, "trade_date"])
    out["hist_yield_obs"] = int(len(valid))
    out["yield_pctile_all"] = current_yield_percentile(valid["hist_yield_ttm"], current_yield)
    for label, days in [("1y", 365), ("2y", 365 * 2), ("5y", 365 * 5), ("10y", 365 * 10)]:
        subset = valid[valid["trade_ts"] >= current_ts - pd.Timedelta(days=days)]
        out[f"yield_pctile_{label}"] = current_yield_percentile(subset["hist_yield_ttm"], current_yield)
    out["yield_to_hist_max"] = current_yield / out["hist_yield_max"] if out["hist_yield_max"] else np.nan
    out["yield_position_label"] = yield_position_label(out["yield_pctile_all"], out["hist_yield_obs"])
    return out


def rank_pct(series: pd.Series, high_good: bool) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    ranked = s.rank(pct=True, ascending=True if high_good else False)
    return ranked.fillna(0.0)


def fmt_pct(x) -> str:
    if pd.isna(x) or math.isinf(float(x)):
        return ""
    return f"{float(x) * 100:.2f}%"


def fmt_num(x, digits: int = 3) -> str:
    if pd.isna(x) or math.isinf(float(x)):
        return ""
    return f"{float(x):.{digits}f}"


def yi(x) -> str:
    if pd.isna(x) or math.isinf(float(x)):
        return ""
    return f"{float(x) / 1e8:.2f}亿"


def esc(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return html.escape(str(value), quote=True)


def render_html(df: pd.DataFrame, generated_at: str, quote_range: str) -> str:
    cards = []
    for i, row in df.head(80).iterrows():
        cards.append(
            f"""
<article class="card">
  <div class="head"><b>#{int(row['rank'])}</b><span>{esc(row['ts_code'])} {esc(row['name'])}</span><em>{fmt_num(row['score'], 1)}</em></div>
  <div class="grid">
    <div><label>实时股息率</label><strong>{fmt_pct(row['dividend_yield_ttm'])}</strong></div>
    <div><label>历史最高股息率</label><strong>{fmt_pct(row['hist_yield_max'])}</strong></div>
    <div><label>成立以来分位</label><strong>{fmt_pct(row['yield_pctile_all'])}</strong></div>
    <div><label>历史样本</label><strong>{int(row['hist_yield_obs']) if pd.notna(row['hist_yield_obs']) else 0}天</strong></div>
    <div><label>估值提示</label><strong>{esc(row['yield_position_label'])}</strong></div>
    <div><label>现价</label><strong>{fmt_num(row['price'], 3)}</strong></div>
    <div><label>TTM分红</label><strong>{fmt_num(row['div_cash_ttm'], 4)}</strong></div>
    <div><label>成交额</label><strong>{yi(row['amount_cny'])}</strong></div>
    <div><label>总费率</label><strong>{fmt_pct(row['total_fee_rate'])}</strong></div>
    <div><label>折溢价</label><strong>{fmt_pct(row['premium_rate'])}</strong></div>
    <div><label>近3年分红年数</label><strong>{int(row['div_years_3y'])}/3</strong></div>
    <div><label>策略</label><strong>{esc(row['strategy_tag'])}</strong></div>
  </div>
  <p>报价：{esc(row['quote_date'])} {esc(row['quote_time'])}；NAV：{esc(row['nav_date'])} / {fmt_num(row['unit_nav'], 4)}；管理人：{esc(row['management'])}</p>
</article>"""
        )
    table = df.head(120)[
        [
            "rank",
            "ts_code",
            "name",
            "strategy_tag",
            "price",
            "dividend_yield_ttm",
            "hist_yield_max",
            "hist_yield_max_date",
            "yield_pctile_all",
            "yield_pctile_1y",
            "yield_pctile_2y",
            "yield_pctile_5y",
            "yield_pctile_10y",
            "hist_yield_obs",
            "yield_position_label",
            "div_cash_ttm",
            "amount_cny",
            "total_fee_rate",
            "premium_rate",
            "div_years_3y",
            "score",
        ]
    ].copy()
    table["price"] = table["price"].map(lambda x: fmt_num(x, 3))
    for col in ["dividend_yield_ttm", "hist_yield_max", "yield_pctile_all", "yield_pctile_1y", "yield_pctile_2y", "yield_pctile_5y", "yield_pctile_10y"]:
        table[col] = table[col].map(fmt_pct)
    table["div_cash_ttm"] = table["div_cash_ttm"].map(lambda x: fmt_num(x, 4))
    table["amount_cny"] = table["amount_cny"].map(yi)
    for col in ["total_fee_rate", "premium_rate"]:
        table[col] = table[col].map(fmt_pct)
    table["score"] = table["score"].map(lambda x: fmt_num(x, 1))
    table.rename(
        columns={
            "rank": "排名",
            "ts_code": "代码",
            "name": "名称",
            "strategy_tag": "策略",
            "price": "现价",
            "dividend_yield_ttm": "实时股息率",
            "hist_yield_max": "历史最高股息率",
            "hist_yield_max_date": "最高日期",
            "yield_pctile_all": "成立以来分位",
            "yield_pctile_1y": "1年分位",
            "yield_pctile_2y": "2年分位",
            "yield_pctile_5y": "5年分位",
            "yield_pctile_10y": "10年分位",
            "hist_yield_obs": "历史样本天数",
            "yield_position_label": "估值提示",
            "div_cash_ttm": "TTM分红/份",
            "amount_cny": "实时成交额",
            "total_fee_rate": "总费率",
            "premium_rate": "折溢价",
            "div_years_3y": "近3年分红年数",
            "score": "综合分",
        },
        inplace=True,
    )
    table_html = table.to_html(index=False, escape=True, classes="rank-table")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>沪深上市红利ETF实时股息率</title>
<style>
body{{margin:0;background:#f7f8fa;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',Arial,sans-serif}}
header{{position:sticky;top:0;background:rgba(247,248,250,.94);backdrop-filter:blur(10px);border-bottom:1px solid #dbe1e8;padding:12px 14px;z-index:2}}
h1{{font-size:20px;margin:0 0 4px}}.meta{{color:#637083;font-size:13px;line-height:1.45}}main{{max-width:1180px;margin:0 auto;padding:14px}}
.links{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 16px}}.links a{{background:#176b87;color:white;text-decoration:none;padding:8px 10px;border-radius:6px;font-size:14px}}
.card{{background:white;border:1px solid #dbe1e8;border-radius:8px;padding:12px;margin:10px 0}}.head{{display:flex;gap:8px;align-items:center}}.head b{{font-size:20px;min-width:42px}}.head span{{font-weight:700;flex:1}}.head em{{font-style:normal;color:#176b87;font-weight:700}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}}.grid div{{background:#f3f6f8;border-radius:6px;padding:8px;min-width:0}}label{{display:block;color:#637083;font-size:12px;margin-bottom:3px}}strong{{font-size:14px;overflow-wrap:anywhere}}p{{color:#3d4a5c;font-size:13px;line-height:1.5}}
.table-wrap{{overflow:auto;background:white;border:1px solid #dbe1e8;border-radius:8px;margin-top:18px}}table{{width:100%;border-collapse:collapse;font-size:13px;min-width:1480px}}th,td{{padding:8px;border-bottom:1px solid #dbe1e8;text-align:right;white-space:nowrap}}th:nth-child(2),th:nth-child(3),th:nth-child(4),th:nth-child(12),td:nth-child(2),td:nth-child(3),td:nth-child(4),td:nth-child(12){{text-align:left}}th{{position:sticky;top:0;background:#eef2f5}}
@media(min-width:780px){{.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card{{margin:0}}.grid{{grid-template-columns:repeat(4,minmax(0,1fr))}}}}
</style></head><body><header><h1>沪深上市红利ETF实时股息率</h1><div class="meta">生成：{generated_at}；实时行情：{quote_range}。覆盖在沪深交易所上市的红利/高股息 ETF，含恒生、港股通、H 股相关红利 ETF。股息率 = 近12个月每份现金分红 / 实时价格；历史分位按 Tushare fund_daily 每日收盘价与当日滚动 TTM 分红重算，分位越低通常代表当前股息率越低、价格相对股息回报越贵。</div></header>
<main><div class="links"><a href="index.html">返回港股主榜</a><a href="a_dividend_etf_rank.csv">CSV</a><a href="a_dividend_etf_rank.md">Markdown</a></div><section class="cards">{''.join(cards)}</section><section class="table-wrap">{table_html}</section></main></body></html>"""


def render_md(df: pd.DataFrame, generated_at: str, quote_range: str) -> str:
    show = df.head(120)[
        [
            "rank",
            "ts_code",
            "name",
            "strategy_tag",
            "price",
            "dividend_yield_ttm",
            "hist_yield_max",
            "hist_yield_max_date",
            "yield_pctile_all",
            "yield_pctile_1y",
            "yield_pctile_2y",
            "yield_pctile_5y",
            "yield_pctile_10y",
            "hist_yield_obs",
            "yield_position_label",
            "div_cash_ttm",
            "amount_cny",
            "total_fee_rate",
            "premium_rate",
            "div_years_3y",
            "score",
        ]
    ].copy()
    show["price"] = show["price"].map(lambda x: fmt_num(x, 3))
    for col in ["dividend_yield_ttm", "hist_yield_max", "yield_pctile_all", "yield_pctile_1y", "yield_pctile_2y", "yield_pctile_5y", "yield_pctile_10y"]:
        show[col] = show[col].map(fmt_pct)
    show["div_cash_ttm"] = show["div_cash_ttm"].map(lambda x: fmt_num(x, 4))
    show["amount_cny"] = show["amount_cny"].map(yi)
    for col in ["total_fee_rate", "premium_rate"]:
        show[col] = show[col].map(fmt_pct)
    show["score"] = show["score"].map(lambda x: fmt_num(x, 1))
    return "\n".join(
        [
            "# 沪深上市红利ETF实时股息率与多维排名",
            "",
            f"- 生成时间: {generated_at}",
            f"- 实时行情范围: {quote_range}",
            "- 覆盖在沪深交易所上市的红利/高股息 ETF，含恒生、港股通、H 股相关红利 ETF。",
            "- 股息率 = 近12个月每份现金分红 / 实时价格；未分红 ETF 的 TTM 股息率为 0。",
            "- 历史股息率 = 历史当日过去365天每份现金分红 / 当日收盘价；当前分位越低，通常代表当前股息率越低、价格相对股息回报越贵。",
            "- 综合分 = 股息率35% + 流动性20% + 费率15% + 折溢价10% + 近3年分红连续性10% + 上市年限10%。",
            "",
            df_to_markdown(show, index=False),
            "",
        ]
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    asof = pd.Timestamp(now.date())
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    start_3y = (asof - pd.Timedelta(days=365 * 3 + 20)).strftime("%Y%m%d")
    start_nav = (asof - pd.Timedelta(days=21)).strftime("%Y%m%d")
    end_date = asof.strftime("%Y%m%d")

    pro = ts.pro_api()
    basic = pro.fund_basic(market="E")
    basic = basic[basic["status"].fillna("").eq("L")].copy()
    basic["list_ts"] = basic["list_date"].map(date_num)
    basic = basic[basic["list_ts"].notna() & basic["list_ts"].le(asof)].copy()
    basic = basic[basic.apply(is_dividend_etf, axis=1)].copy()
    basic["strategy_tag"] = basic.apply(strategy_tag, axis=1)
    basic.to_csv(OUT / "a_dividend_etf_universe.csv", index=False)

    quotes = fetch_sina_quotes(basic["ts_code"].astype(str).tolist())
    if quotes.empty:
        raise SystemExit("No Sina realtime ETF quotes fetched")
    quotes.to_csv(OUT / "a_dividend_etf_realtime_quotes.csv", index=False)

    rows = []
    for _, fund in basic.iterrows():
        code = str(fund["ts_code"])
        div_events = fetch_dividend_events(pro, code)
        div = dividend_stats_from_events(div_events, asof)
        nav = latest_nav(pro, code, start_nav, end_date)
        qhit = quotes.loc[quotes["ts_code"].eq(code)].head(1)
        if not qhit.empty:
            current_price = pd.to_numeric(qhit.iloc[0].get("price"), errors="coerce")
            quote_date = str(qhit.iloc[0].get("quote_date") or "")
        else:
            current_price = np.nan
            quote_date = ""
        hist = historical_yield_stats(
            pro,
            code,
            str(fund.get("list_date") or ""),
            end_date,
            asof,
            div_events,
            current_price,
            float(div["div_cash_ttm"]),
            quote_date,
        )
        row = fund.to_dict()
        row.update(div)
        row.update(nav)
        row.update(hist)
        rows.append(row)
        time.sleep(0.03)

    df = pd.DataFrame(rows).merge(quotes, on="ts_code", how="left")
    for col in ["price", "amount_cny", "m_fee", "c_fee", "unit_nav"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["total_fee_rate"] = (df["m_fee"].fillna(0) + df["c_fee"].fillna(0)) / 100.0
    df["dividend_yield_ttm"] = df["div_cash_ttm"] / df["price"].replace(0, np.nan)
    df["premium_rate"] = df["price"] / df["unit_nav"].replace(0, np.nan) - 1
    df["list_ts"] = df["list_date"].map(date_num)
    df["age_years"] = (asof - df["list_ts"]).dt.days / 365.25
    df["stability_score"] = (pd.to_numeric(df["div_years_3y"], errors="coerce").fillna(0) / 3.0).clip(0, 1)
    df["premium_quality"] = -df["premium_rate"].abs().clip(upper=0.05)

    df["yield_rank"] = rank_pct(df["dividend_yield_ttm"], high_good=True)
    df["liquidity_rank"] = rank_pct(np.log1p(df["amount_cny"]), high_good=True)
    df["fee_rank"] = rank_pct(df["total_fee_rate"], high_good=False)
    df["premium_rank"] = rank_pct(df["premium_quality"], high_good=True)
    df["age_rank"] = rank_pct(df["age_years"], high_good=True)
    df["score"] = (
        df["yield_rank"] * 35
        + df["liquidity_rank"] * 20
        + df["fee_rank"] * 15
        + df["premium_rank"] * 10
        + df["stability_score"] * 10
        + df["age_rank"] * 10
    )
    df = df.sort_values(["score", "dividend_yield_ttm", "amount_cny"], ascending=[False, False, False]).reset_index(drop=True)
    df["rank"] = np.arange(1, len(df) + 1)

    quote_times = (df["quote_date"].fillna("") + " " + df["quote_time"].fillna("")).str.strip()
    quote_times = quote_times[quote_times.ne("")]
    quote_range = f"{quote_times.min()} 至 {quote_times.max()}" if len(quote_times) else "无"

    history_cols = [
        "rank",
        "ts_code",
        "name",
        "strategy_tag",
        "price",
        "dividend_yield_ttm",
        "hist_yield_max",
        "hist_yield_max_date",
        "hist_yield_obs",
        "yield_pctile_all",
        "yield_pctile_1y",
        "yield_pctile_2y",
        "yield_pctile_5y",
        "yield_pctile_10y",
        "yield_to_hist_max",
        "yield_position_label",
        "list_date",
        "last_div_date",
    ]
    df[[c for c in history_cols if c in df.columns]].to_csv(OUT / "a_dividend_etf_yield_history_stats.csv", index=False)

    ordered_cols = [
        "rank",
        "ts_code",
        "name",
        "strategy_tag",
        "management",
        "price",
        "quote_date",
        "quote_time",
        "div_cash_ttm",
        "dividend_yield_ttm",
        "hist_yield_max",
        "hist_yield_max_date",
        "hist_yield_obs",
        "yield_pctile_all",
        "yield_pctile_1y",
        "yield_pctile_2y",
        "yield_pctile_5y",
        "yield_pctile_10y",
        "yield_to_hist_max",
        "yield_position_label",
        "div_count_ttm",
        "div_cash_3y",
        "div_count_3y",
        "div_years_3y",
        "last_div_date",
        "amount_cny",
        "m_fee",
        "c_fee",
        "total_fee_rate",
        "unit_nav",
        "nav_date",
        "premium_rate",
        "list_date",
        "age_years",
        "score",
        "yield_rank",
        "liquidity_rank",
        "fee_rank",
        "premium_rank",
        "age_rank",
        "benchmark",
        "quote_source",
    ]
    cols = [c for c in ordered_cols if c in df.columns] + [c for c in df.columns if c not in ordered_cols]
    df[cols].to_csv(OUT / "a_dividend_etf_rank.csv", index=False)
    (OUT / "a_dividend_etf_rank.md").write_text(render_md(df, generated_at, quote_range), encoding="utf-8")
    (OUT / "a_dividend_etf_rank.html").write_text(render_html(df, generated_at, quote_range), encoding="utf-8")

    print(f"Dividend ETF universe: {len(basic)}")
    print(f"Realtime quotes: {len(quotes)}")
    print(f"Quote range: {quote_range}")
    print(OUT / "a_dividend_etf_rank.csv")
    print(OUT / "a_dividend_etf_rank.html")


if __name__ == "__main__":
    main()
