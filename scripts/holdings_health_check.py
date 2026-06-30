#!/usr/bin/env python3
"""Build a multi-market health check for the handwritten holdings list.

The source holdings table is manual/OCR output. This script standardizes
security codes, refreshes market data, estimates cost from current price and
the handwritten P/L percentage, then adds dividend, financial-trend and recent
announcement checks.

Secrets are intentionally not read from files or written to outputs. Tushare is
loaded through the local tushare.pro_api() configuration.
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import tushare as ts


ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", ROOT)).resolve()
CACHE = OUT / "holdings_health_cache"
ASIA = ZoneInfo("Asia/Shanghai")
ASOF = datetime.now(ASIA).strftime("%Y-%m-%d")
UA = {"User-Agent": "Mozilla/5.0"}

SINA_URL = "https://hq.sinajs.cn/list={symbols}"
TENCENT_URL = "https://qt.gtimg.cn/q={symbols}"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FRANKFURTER = "https://api.frankfurter.app/latest?from=CNY&to=HKD,USD,SGD"
HKEXNEWS_ACTIVE = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
HKEXNEWS_TITLE = "https://www1.hkexnews.hk/search/titleSearchServlet.do"

RISK_KEYWORDS = (
    "减持",
    "质押",
    "冻结",
    "诉讼",
    "仲裁",
    "处罚",
    "监管",
    "问询",
    "立案",
    "调查",
    "退市",
    "终止上市",
    "风险提示",
    "亏损",
    "下滑",
    "下降",
    "商誉减值",
    "内幕",
)


@dataclass(frozen=True)
class Instrument:
    row_no: int
    std_code: str
    official_name: str
    market: str
    asset_class: str
    quote_currency: str
    quote_source: str
    financial_code: str = ""
    financial_market: str = ""
    theme: str = ""
    notes: str = ""


INSTRUMENTS: dict[int, Instrument] = {
    1: Instrument(1, "900926.SH", "宝信B", "B股", "B股", "USD", "sina", "600845.SH", "A股", "B/A折价"),
    2: Instrument(2, "600690.SH", "海尔智家A", "A股", "A股", "CNY", "sina", "600690.SH", "A股", "家电出海"),
    3: Instrument(3, "600177.SH", "雅戈尔", "A股", "A股", "CNY", "sina", "600177.SH", "A股", "红利消费"),
    4: Instrument(4, "000423.SZ", "东阿阿胶", "A股", "A股", "CNY", "sina", "000423.SZ", "A股", "中药消费"),
    5: Instrument(5, "000651.SZ", "格力电器", "A股", "A股", "CNY", "sina", "000651.SZ", "A股", "家电红利"),
    6: Instrument(6, "200596.SZ", "古井贡B", "B股", "B股", "HKD", "sina", "000596.SZ", "A股", "B/A折价"),
    7: Instrument(7, "00992.HK", "联想集团", "港股", "港股", "HKD", "tencent", "00992.HK", "港股", "科技硬件"),
    8: Instrument(8, "603816.SH", "顾家家居", "A股", "A股", "CNY", "sina", "603816.SH", "A股", "地产链消费"),
    9: Instrument(9, "600298.SH", "安琪酵母", "A股", "A股", "CNY", "sina", "600298.SH", "A股", "食品出海"),
    10: Instrument(10, "600741.SH", "华域汽车A", "A股", "A股", "CNY", "sina", "600741.SH", "A股", "汽车零部件"),
    11: Instrument(11, "601058.SH", "赛轮轮胎A", "A股", "A股", "CNY", "sina", "601058.SH", "A股", "轮胎出海"),
    12: Instrument(12, "603259.SH", "药明康德A", "A股", "A股", "CNY", "sina", "603259.SH", "A股", "医药CXO"),
    13: Instrument(13, "02313.HK", "申洲国际", "港股", "港股", "HKD", "tencent", "02313.HK", "港股", "纺织代工"),
    14: Instrument(14, "515180.SH", "易方达中证红利ETF", "ETF", "ETF", "CNY", "sina", "515180.SH", "ETF", "红利ETF"),
    15: Instrument(15, "000921.SZ", "海信家电A", "A股", "A股", "CNY", "sina", "000921.SZ", "A股", "家电出海"),
    16: Instrument(16, "600941.SH", "中国移动A", "A股", "A股", "CNY", "sina", "600941.SH", "A股", "通信公用"),
    17: Instrument(17, "000807.SZ", "云铝股份A", "A股", "A股", "CNY", "sina", "000807.SZ", "A股", "铝周期"),
    18: Instrument(18, "00728.HK", "中国电信", "港股", "港股", "HKD", "tencent", "00728.HK", "港股", "通信公用"),
    19: Instrument(19, "603993.SH", "洛阳钼业A", "A股", "A股", "CNY", "sina", "603993.SH", "A股", "资源周期"),
    20: Instrument(20, "601899.SH", "紫金矿业A", "A股", "A股", "CNY", "sina", "601899.SH", "A股", "资源周期"),
    21: Instrument(21, "600660.SH", "福耀玻璃", "A股", "A股", "CNY", "sina", "600660.SH", "A股", "汽车玻璃"),
    22: Instrument(22, "000333.SZ", "美的集团", "A股", "A股", "CNY", "sina", "000333.SZ", "A股", "家电红利"),
    23: Instrument(23, "159307.SZ", "博时中证红利低波动100ETF", "ETF", "ETF", "CNY", "sina", "159307.SZ", "ETF", "红利ETF"),
    24: Instrument(24, "09926.HK", "康方生物", "港股", "港股", "HKD", "tencent", "09926.HK", "港股", "创新药"),
    25: Instrument(25, "PCT.SI", "栢能集团", "新加坡股", "新加坡股", "SGD", "yahoo", "", "", "AI硬件", "非港股，Tushare覆盖有限"),
    26: Instrument(26, "09618.HK", "京东集团-SW", "港股", "港股", "HKD", "tencent", "09618.HK", "港股", "互联网回购"),
    27: Instrument(27, "00883.HK", "中国海洋石油", "港股", "港股", "HKD", "tencent", "00883.HK", "港股", "能源红利"),
    28: Instrument(28, "00799.HK", "IGG", "港股", "港股", "HKD", "tencent", "00799.HK", "港股", "游戏股东回报"),
    29: Instrument(29, "02669.HK", "中海物业", "港股", "港股", "HKD", "tencent", "02669.HK", "港股", "物业地产链", "原表代码写作02069，按官方代码02669计算"),
    30: Instrument(30, "01024.HK", "快手-W", "港股", "港股", "HKD", "tencent", "01024.HK", "港股", "互联网AI"),
    31: Instrument(31, "01023.HK", "时代集团控股", "港股", "港股", "HKD", "tencent", "01023.HK", "港股", "特殊高息"),
    32: Instrument(32, "02156.HK", "建发物业", "港股", "港股", "HKD", "tencent", "02156.HK", "港股", "物业地产链"),
    33: Instrument(33, "01995.HK", "永升服务", "港股", "港股", "HKD", "tencent", "01995.HK", "港股", "物业地产链"),
    34: Instrument(34, "03900.HK", "绿城中国", "港股", "港股", "HKD", "tencent", "03900.HK", "港股", "地产链"),
    35: Instrument(35, "06049.HK", "保利物业", "港股", "港股", "HKD", "tencent", "06049.HK", "港股", "物业地产链"),
}


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False


def to_float(value: Any) -> float:
    if value in ("", None, "-"):
        return np.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def parse_pct(value: Any) -> float:
    text = str(value or "").strip().replace("%", "").replace("+", "")
    return to_float(text) / 100.0


def pct_text(value: Any, digits: int = 1) -> str:
    value = to_float(value)
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value * 100:.{digits}f}%"


def num_text(value: Any, digits: int = 2) -> str:
    value = to_float(value)
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.{digits}f}"


def money_text(value: Any, currency: str = "", digits: int = 2) -> str:
    value = to_float(value)
    if math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.{digits}f}{currency}"


def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def esc(value: Any) -> str:
    if is_missing(value):
        return ""
    return html.escape(str(value), quote=True)


def sh_sz_symbol(ts_code: str) -> str:
    code, exchange = str(ts_code).split(".")
    return ("sh" if exchange == "SH" else "sz") + code


def code5(ts_code: str) -> str:
    return str(ts_code).split(".")[0].zfill(5)


def latest_open_day(pro, exchange: str, end_date: str) -> str:
    start = (pd.Timestamp(end_date) - pd.Timedelta(days=20)).strftime("%Y%m%d")
    end = pd.Timestamp(end_date).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange=exchange, start_date=start, end_date=end)
    if cal.empty:
        return end
    days = cal.loc[cal["is_open"].astype(str).isin(["1", "True", "true"]), "cal_date"].astype(str)
    return sorted(days, reverse=True)[0]


def fetch_fx() -> tuple[dict[str, float], str]:
    fallback = {"CNY": 1.0, "HKD": 0.8653, "USD": 6.7850, "SGD": 5.2390}
    try:
        resp = requests.get(FRANKFURTER, timeout=15, headers=UA)
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates") or {}
        out = {"CNY": 1.0}
        for cur in ("HKD", "USD", "SGD"):
            if rates.get(cur):
                out[cur] = 1.0 / float(rates[cur])
        for cur, val in fallback.items():
            out.setdefault(cur, val)
        return out, str(data.get("date") or "")
    except Exception:
        return fallback, "fallback"


def parse_sina_line(line: str) -> dict[str, Any] | None:
    if '="' not in line:
        return None
    left, body = line.split('="', 1)
    symbol = left.rsplit("_", 1)[-1]
    fields = body.rstrip('";').split(",")
    if len(fields) < 32 or not fields[0]:
        return None
    exchange = "SH" if symbol.startswith("sh") else "SZ"
    code = symbol[2:]
    price = to_float(fields[3])
    if math.isnan(price) or price <= 0:
        price = to_float(fields[2])
    return {
        "ts_code": f"{code}.{exchange}",
        "quote_name": fields[0],
        "price": price,
        "open": to_float(fields[1]),
        "pre_close": to_float(fields[2]),
        "high": to_float(fields[4]),
        "low": to_float(fields[5]),
        "volume": to_float(fields[8]),
        "amount": to_float(fields[9]),
        "quote_time": f"{fields[30]} {fields[31]}" if len(fields) > 31 else "",
        "source": "sina",
    }


def fetch_sina_quotes(codes: list[str]) -> pd.DataFrame:
    symbols = [sh_sz_symbol(c) for c in codes]
    rows: list[dict[str, Any]] = []
    session = requests.Session()
    session.headers.update({"Referer": "https://finance.sina.com.cn", **UA})
    for i in range(0, len(symbols), 70):
        resp = session.get(SINA_URL.format(symbols=",".join(symbols[i : i + 70])), timeout=20)
        resp.raise_for_status()
        resp.encoding = "gb18030"
        for line in resp.text.splitlines():
            parsed = parse_sina_line(line)
            if parsed:
                rows.append(parsed)
        time.sleep(0.05)
    return pd.DataFrame(rows).drop_duplicates("ts_code", keep="last") if rows else pd.DataFrame()


def parse_tencent_line(body: str) -> dict[str, Any] | None:
    fields = body.split("~")
    if len(fields) < 46:
        return None
    symbol = str(fields[2]).zfill(5)
    price = to_float(fields[3])
    if math.isnan(price) or price <= 0:
        return None
    return {
        "ts_code": f"{symbol}.HK",
        "quote_name": fields[1],
        "price": price,
        "pre_close": to_float(fields[4]),
        "open": to_float(fields[5]),
        "volume": to_float(fields[36] if len(fields) > 36 else np.nan),
        "amount": to_float(fields[37] if len(fields) > 37 else np.nan),
        "quote_time": fields[30] if len(fields) > 30 else "",
        "pct_chg": to_float(fields[32] if len(fields) > 32 else np.nan) / 100.0,
        "high": to_float(fields[33] if len(fields) > 33 else np.nan),
        "low": to_float(fields[34] if len(fields) > 34 else np.nan),
        "pe_ttm": to_float(fields[39] if len(fields) > 39 else np.nan),
        "pb": to_float(fields[43] if len(fields) > 43 else np.nan),
        # Tencent HK fields 44/45 are floating/total market cap in HKD 100m.
        # Large H-share issuers make the distinction obvious: 00728.HK reports
        # ~592.6 / ~3907.4, where the latter is the total market cap.
        "float_mv": to_float(fields[44] if len(fields) > 44 else np.nan) * 1e8,
        "total_mv": to_float(fields[45] if len(fields) > 45 else np.nan) * 1e8,
        "source": "tencent",
    }


def fetch_tencent_quotes(codes: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    session = requests.Session()
    session.headers.update(UA)
    symbols = [f"hk{code5(c)}" for c in codes]
    for i in range(0, len(symbols), 80):
        resp = session.get(TENCENT_URL.format(symbols=",".join(symbols[i : i + 80])), timeout=20)
        resp.raise_for_status()
        for part in resp.text.split(";\n"):
            if '="' not in part:
                continue
            body = part.split('="', 1)[1].rstrip('";')
            parsed = parse_tencent_line(body)
            if parsed:
                rows.append(parsed)
        time.sleep(0.05)
    return pd.DataFrame(rows).drop_duplicates("ts_code", keep="last") if rows else pd.DataFrame()


def fetch_yahoo_quote(symbol: str) -> dict[str, Any]:
    try:
        resp = requests.get(
            YAHOO_CHART.format(symbol=symbol),
            params={"range": "1mo", "interval": "1d", "events": "div"},
            timeout=20,
            headers=UA,
        )
        resp.raise_for_status()
        result = (resp.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return {}
        meta = result.get("meta") or {}
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = [x for x in quote.get("close", []) if x is not None]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else np.nan)
        ts_val = meta.get("regularMarketTime")
        qtime = ""
        if ts_val:
            qtime = datetime.fromtimestamp(int(ts_val), ZoneInfo(meta.get("exchangeTimezoneName", "Asia/Singapore"))).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
        divs = result.get("events", {}).get("dividends", {}) if isinstance(result.get("events"), dict) else {}
        div_cash = 0.0
        cutoff = pd.Timestamp.now(tz=ASIA) - pd.Timedelta(days=365)
        for item in divs.values():
            dt = pd.to_datetime(item.get("date"), unit="s", utc=True).tz_convert(ASIA)
            if dt >= cutoff:
                div_cash += float(item.get("amount") or 0)
        return {
            "ts_code": symbol,
            "quote_name": meta.get("longName") or symbol,
            "price": float(price),
            "quote_time": qtime,
            "currency": meta.get("currency") or "SGD",
            "div_cash_ttm": div_cash,
            "source": "yahoo",
        }
    except Exception:
        return {}


def clean_tushare(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if col not in {"ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type", "end_type", "div_proc", "record_date", "ex_date", "pay_date", "name", "title", "url"}:
            out[col] = pd.to_numeric(out[col], errors="ignore")
    return out


def cache_csv(name: str, fetcher, refresh: bool = False) -> pd.DataFrame:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{name}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"ann_date": str, "end_date": str, "trade_date": str, "ex_date": str})
    df = fetcher()
    if not df.empty:
        df.to_csv(path, index=False)
    return df


def fetch_cn_bundle(pro, code: str, refresh: bool = False) -> dict[str, pd.DataFrame]:
    start = "20220101"
    end = datetime.now(ASIA).strftime("%Y%m%d")
    safe = code.replace(".", "_")
    return {
        "income": cache_csv(f"cn_income_{safe}", lambda: pro.income(ts_code=code, start_date=start, end_date=end), refresh),
        "cashflow": cache_csv(f"cn_cashflow_{safe}", lambda: pro.cashflow(ts_code=code, start_date=start, end_date=end), refresh),
        "balancesheet": cache_csv(f"cn_balancesheet_{safe}", lambda: pro.balancesheet(ts_code=code, start_date=start, end_date=end), refresh),
        "fina_indicator": cache_csv(f"cn_fina_{safe}", lambda: pro.fina_indicator(ts_code=code, start_date=start, end_date=end), refresh),
        "dividend": cache_csv(
            f"cn_dividend_{safe}",
            lambda: pro.dividend(
                ts_code=code,
                fields="ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date",
            ),
            refresh,
        ),
        "anns": cache_csv(f"cn_anns_{safe}", lambda: pro.anns_d(ts_code=code, start_date="20260401", end_date=end), refresh),
    }


def pivot_hk_statement(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    cleaned = df.copy()
    cleaned["ind_value"] = pd.to_numeric(cleaned["ind_value"], errors="coerce")
    return (
        cleaned.dropna(subset=["end_date", "ind_name"])
        .pivot_table(index="end_date", columns="ind_name", values="ind_value", aggfunc="first")
        .sort_index()
    )


def fetch_hk_bundle(pro, code: str, refresh: bool = False) -> dict[str, pd.DataFrame]:
    safe = code.replace(".", "_")

    def hk_endpoint(ep: str) -> pd.DataFrame:
        raw = cache_csv(f"{ep}_{safe}", lambda: pro.query(ep, ts_code=code, limit=8000), refresh)
        return pivot_hk_statement(raw)

    return {
        "hk_income": hk_endpoint("hk_income"),
        "hk_cashflow": hk_endpoint("hk_cashflow"),
        "hk_balancesheet": hk_endpoint("hk_balancesheet"),
    }


def dedupe_reports(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "end_date" not in df.columns:
        return df
    out = df.copy()
    out["end_date"] = out["end_date"].astype(str)
    if "ann_date" in out.columns:
        out["ann_date"] = out["ann_date"].astype(str)
        out = out.sort_values(["end_date", "ann_date"], ascending=[True, False])
    else:
        out = out.sort_values("end_date")
    return out.drop_duplicates("end_date", keep="first").sort_values("end_date")


def annual_cn(df: pd.DataFrame) -> pd.DataFrame:
    df = dedupe_reports(df)
    if df.empty:
        return df
    dec = df[df["end_date"].astype(str).str.endswith("1231")]
    return dec if len(dec) >= 2 else df.groupby(df["end_date"].astype(str).str[:4]).tail(1)


def annual_hk(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df.index = df.index.astype(str)
    key_candidates = [
        "营业额",
        "经营收入",
        "经营收入总额",
        "营运收入",
        "股东应占溢利",
        "经营业务现金净额",
        "总资产",
    ]
    key = next((c for c in key_candidates if c in df.columns), None)
    if key:
        tmp = df.copy()
        tmp["_year"] = tmp.index.str[:4]
        tmp["_annual_key"] = pd.to_numeric(tmp[key], errors="coerce").abs()
        idx = tmp.sort_values(["_year", "_annual_key"]).groupby("_year").tail(1).index
        selected = df.loc[idx].sort_index()
        monthdays = selected.index.str[4:8]
        if len(monthdays) >= 3:
            dominant = monthdays.value_counts().idxmax()
            stable = selected[monthdays == dominant]
            if len(stable) >= 2:
                return stable
        return selected
    return df.groupby(df.index.str[:4]).tail(1)


def first_present(row: pd.Series, names: list[str]) -> float:
    for name in names:
        if name in row.index and pd.notna(row[name]):
            return float(row[name])
    return np.nan


def sum_present(row: pd.Series, names: list[str]) -> float:
    vals = [float(row[n]) for n in names if n in row.index and pd.notna(row[n])]
    return float(np.nansum(vals)) if vals else np.nan


HK_INCOME = {
    "revenue": ["营业额", "经营收入", "经营收入总额", "营运收入"],
    "net_profit": ["股东应占溢利"],
    "dps": ["每股股息"],
    "other_gains": ["其他收益", "其他收入", "重估盈余", "按公平价值列账的金融资产公平值增加/减少", "出售资产之溢利"],
}
HK_CF = {"cfo": ["经营业务现金净额"], "div_paid": ["已付股息(融资)"], "buyback": ["回购股份"]}
HK_BS = {"total_liab": ["总负债"], "equity": ["股东权益", "股东权益合计", "归属于母公司股东权益", "净资产"]}


def cagr(values: pd.Series) -> float:
    s = pd.to_numeric(values, errors="coerce").dropna()
    if len(s) < 2:
        return np.nan
    first, last = float(s.iloc[0]), float(s.iloc[-1])
    n = len(s) - 1
    if first <= 0 or last <= 0 or n <= 0:
        return np.nan
    return (last / first) ** (1 / n) - 1


def trend_label(values: pd.Series) -> str:
    s = pd.to_numeric(values, errors="coerce").dropna()
    if len(s) < 2:
        return "样本不足"
    latest = float(s.iloc[-1])
    prev = float(s.iloc[-2])
    high = float(s.max())
    if latest <= 0:
        return "亏损/转亏"
    if prev > 0 and latest < prev * 0.75:
        return "明显下滑"
    if high > 0 and latest < high * 0.65:
        return "高点回落"
    if prev > 0 and latest > prev * 1.15:
        return "增长"
    return "稳定"


def cn_financial_metrics(bundle: dict[str, pd.DataFrame], daily_basic: pd.Series | None, price: float, currency: str, fx: dict[str, float]) -> dict[str, Any]:
    inc = annual_cn(bundle["income"]).tail(4)
    cf = annual_cn(bundle["cashflow"]).tail(4)
    fina = dedupe_reports(bundle["fina_indicator"])
    latest_fina = fina.iloc[-1] if not fina.empty else pd.Series(dtype=float)
    latest_annual_fina = annual_cn(fina).iloc[-1] if not annual_cn(fina).empty else latest_fina

    profit = pd.to_numeric(inc.get("n_income_attr_p"), errors="coerce") if "n_income_attr_p" in inc else pd.Series(dtype=float)
    revenue = pd.to_numeric(inc.get("revenue"), errors="coerce") if "revenue" in inc else pd.Series(dtype=float)
    cfo = pd.to_numeric(cf.get("n_cashflow_act"), errors="coerce") if "n_cashflow_act" in cf else pd.Series(dtype=float)
    latest_profit = profit.iloc[-1] if len(profit) else np.nan
    latest_cfo = cfo.iloc[-1] if len(cfo) else np.nan
    cfo_profit = latest_cfo / latest_profit if pd.notna(latest_profit) and latest_profit else np.nan

    dv_ttm = np.nan
    pe_ttm = np.nan
    pb = np.nan
    total_mv = np.nan
    turnover_rate = np.nan
    if daily_basic is not None and not daily_basic.empty:
        dv_ttm = to_float(daily_basic.get("dv_ttm")) / 100.0
        pe_ttm = to_float(daily_basic.get("pe_ttm"))
        pb = to_float(daily_basic.get("pb"))
        total_mv = to_float(daily_basic.get("total_mv")) * 10000
        turnover_rate = to_float(daily_basic.get("turnover_rate")) / 100.0

    div = bundle["dividend"].copy()
    div_cash = np.nan
    div_yield = dv_ttm
    if not div.empty:
        div["cash_div_tax"] = pd.to_numeric(div.get("cash_div_tax"), errors="coerce")
        div["ex_ts"] = pd.to_datetime(div.get("ex_date"), format="%Y%m%d", errors="coerce")
        div_impl = div[~div["div_proc"].astype(str).str.contains("不分配|取消", na=False)].copy()
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=365)
        ttm = div_impl[(div_impl["ex_ts"].notna()) & (div_impl["ex_ts"] >= cutoff)]
        if ttm.empty:
            ttm = div_impl.head(2)
        div_cash = float(ttm["cash_div_tax"].dropna().sum()) if not ttm.empty else np.nan
        if pd.notna(div_cash) and price and price > 0:
            # B-share price is not CNY. Convert CNY cash dividend into quote currency.
            cny_per_quote = fx.get(currency, 1.0)
            div_yield = (div_cash / cny_per_quote) / price

    anns = bundle["anns"].copy()
    ann_titles = []
    risk_titles = []
    if not anns.empty:
        anns = anns.sort_values("ann_date", ascending=False)
        for _, r in anns.head(5).iterrows():
            title = compact(r.get("title"))
            if title:
                ann_titles.append(f"{r.get('ann_date')}: {title}")
            if any(k in title for k in RISK_KEYWORDS):
                risk_titles.append(f"{r.get('ann_date')}: {title}")

    return {
        "report_date": str(latest_annual_fina.get("end_date", "")),
        "latest_report_date": str(latest_fina.get("end_date", "")),
        "revenue_trend": trend_label(revenue),
        "profit_trend": trend_label(profit),
        "profit_cagr": cagr(profit),
        "latest_profit_yoy": to_float(latest_fina.get("netprofit_yoy")) / 100.0,
        "latest_revenue_yoy": to_float(latest_fina.get("or_yoy")) / 100.0,
        "roe": to_float(latest_annual_fina.get("roe")) / 100.0,
        "gross_margin": to_float(latest_annual_fina.get("grossprofit_margin")) / 100.0,
        "debt_to_assets": to_float(latest_annual_fina.get("debt_to_assets")) / 100.0,
        "cfo_profit": cfo_profit,
        "dividend_yield": div_yield,
        "dividend_cash_ttm": div_cash,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "total_mv_quote": total_mv,
        "turnover_rate": turnover_rate,
        "recent_announcements": " || ".join(ann_titles[:3]),
        "risk_announcements": " || ".join(risk_titles[:3]),
        "risk_announcement_count": len(risk_titles),
        "financial_coverage": "Tushare A股财务/分红/公告",
    }


def hk_financial_metrics(bundle: dict[str, pd.DataFrame], price: float) -> dict[str, Any]:
    inc = annual_hk(bundle["hk_income"]).tail(4)
    cf = annual_hk(bundle["hk_cashflow"]).tail(4)
    bs = annual_hk(bundle["hk_balancesheet"]).tail(1)
    revenue = inc.apply(lambda r: first_present(r, HK_INCOME["revenue"]), axis=1) if not inc.empty else pd.Series(dtype=float)
    profit = inc.apply(lambda r: first_present(r, HK_INCOME["net_profit"]), axis=1) if not inc.empty else pd.Series(dtype=float)
    dps = inc.apply(lambda r: first_present(r, HK_INCOME["dps"]), axis=1) if not inc.empty else pd.Series(dtype=float)
    other = inc.apply(lambda r: sum_present(r, HK_INCOME["other_gains"]), axis=1) if not inc.empty else pd.Series(dtype=float)
    cfo = cf.apply(lambda r: first_present(r, HK_CF["cfo"]), axis=1) if not cf.empty else pd.Series(dtype=float)
    div_paid = cf.apply(lambda r: abs(first_present(r, HK_CF["div_paid"])), axis=1) if not cf.empty else pd.Series(dtype=float)
    buyback = cf.apply(lambda r: abs(first_present(r, HK_CF["buyback"])), axis=1) if not cf.empty else pd.Series(dtype=float)

    latest_profit = profit.iloc[-1] if len(profit) else np.nan
    latest_cfo = cfo.iloc[-1] if len(cfo) else np.nan
    latest_dps = dps.iloc[-1] if len(dps) else np.nan
    latest_div_paid = div_paid.iloc[-1] if len(div_paid) else np.nan
    latest_buyback = buyback.iloc[-1] if len(buyback) else np.nan
    cfo_profit = latest_cfo / latest_profit if pd.notna(latest_profit) and latest_profit else np.nan
    div_yield = latest_dps / price if pd.notna(latest_dps) and price and price > 0 else np.nan

    total_liab = np.nan
    equity = np.nan
    if not bs.empty:
        row = bs.iloc[-1]
        total_liab = first_present(row, HK_BS["total_liab"])
        equity = first_present(row, HK_BS["equity"])

    return {
        "report_date": inc.index[-1] if not inc.empty else "",
        "latest_report_date": inc.index[-1] if not inc.empty else "",
        "revenue_trend": trend_label(revenue),
        "profit_trend": trend_label(profit),
        "profit_cagr": cagr(profit),
        "latest_profit_yoy": np.nan,
        "latest_revenue_yoy": np.nan,
        "roe": latest_profit / equity if pd.notna(equity) and equity else np.nan,
        "gross_margin": np.nan,
        "debt_to_assets": total_liab / (total_liab + equity) if pd.notna(total_liab) and pd.notna(equity) and (total_liab + equity) else np.nan,
        "cfo_profit": cfo_profit,
        "dividend_yield": div_yield,
        "dividend_cash_ttm": latest_dps,
        "div_paid_latest": latest_div_paid,
        "buyback_latest": latest_buyback,
        "shareholder_return_latest": np.nansum([latest_div_paid, latest_buyback]),
        "oneoff_profit_ratio": other.iloc[-1] / abs(latest_profit) if len(other) and pd.notna(latest_profit) and latest_profit else np.nan,
        "financial_coverage": "Tushare 港股财报",
    }


def fetch_hkex_stock_map(refresh: bool = False) -> dict[str, dict]:
    path = CACHE / "hkex_active_stock_map.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path)
    else:
        CACHE.mkdir(exist_ok=True)
        resp = requests.get(
            HKEXNEWS_ACTIVE,
            timeout=25,
            headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh", **UA},
        )
        resp.raise_for_status()
        df = pd.DataFrame(resp.json())
        df.to_csv(path, index=False)
    if df.empty:
        return {}
    df["c"] = df["c"].astype(str).str.zfill(5)
    return {str(r["c"]).zfill(5): r for _, r in df.iterrows()}


def fetch_hkex_titles(ts_code: str, stock_map: dict[str, dict], refresh: bool = False) -> pd.DataFrame:
    c = code5(ts_code)
    safe = c
    path = CACHE / f"hkex_titles_{safe}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path)
    stock = stock_map.get(c)
    if stock is None:
        return pd.DataFrame()
    today = datetime.now(ASIA).strftime("%Y%m%d")
    from_date = (pd.Timestamp(today) - pd.Timedelta(days=180)).strftime("%Y%m%d")
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": str(stock["i"]),
        "documentType": "-1",
        "fromDate": from_date,
        "toDate": today,
        "title": "",
        "searchType": "rbAfter2006",
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": "100",
        "lang": "zh",
    }
    try:
        resp = requests.get(
            HKEXNEWS_TITLE,
            params=params,
            timeout=30,
            headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh", **UA},
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("result")
        if not raw or raw == "null":
            return pd.DataFrame()
        items = json.loads(raw)
        rows = []
        for it in items:
            title = compact(it.get("TITLE") or it.get("LONGTEXT") or "")
            rows.append(
                {
                    "date_time": compact(it.get("DATE_TIME") or ""),
                    "title": title,
                    "url": it.get("FILE_LINK") or "",
                    "is_risk": any(k in title for k in RISK_KEYWORDS),
                }
            )
        df = pd.DataFrame(rows)
        if not df.empty:
            df.to_csv(path, index=False)
        return df
    except Exception:
        return pd.DataFrame()


def logic_review(row: dict[str, Any]) -> tuple[str, str, str]:
    logic = str(row.get("买入逻辑", ""))
    flags: list[str] = []
    broken: list[str] = []
    ok: list[str] = []

    div_yield = to_float(row.get("dividend_yield"))
    shareholder_yield = to_float(row.get("shareholder_return_yield"))
    return_yield = np.nanmax([div_yield, shareholder_yield]) if not (pd.isna(div_yield) and pd.isna(shareholder_yield)) else np.nan
    pe = to_float(row.get("pe_ttm"))
    pb = to_float(row.get("pb"))
    profit_yoy = to_float(row.get("latest_profit_yoy"))
    cfo_profit = to_float(row.get("cfo_profit"))
    profit_trend = str(row.get("profit_trend") or "")
    risk_ann_count = int(row.get("risk_announcement_count") or 0)
    theme = str(row.get("theme") or "")

    if any(k in logic for k in ["分红", "股东回报", "股息", "防守"]):
        if pd.notna(return_yield) and return_yield >= 0.04:
            ok.append("股东回报/分红仍有数据支撑")
        elif pd.notna(return_yield) and return_yield >= 0.02:
            flags.append("股息率中等，需看持续性")
        else:
            broken.append("股东回报证据偏弱或暂无TTM股息")

    if any(k in logic for k in ["低估", "估值低", "低估值"]):
        if (pd.notna(pe) and pe > 0 and pe <= 12) or (pd.notna(pb) and pb <= 1.3):
            ok.append("低估值仍有估值指标支撑")
        elif (pd.notna(pe) and pe > 0 and pe <= 20) or (pd.notna(pb) and pb <= 2.0):
            flags.append("估值不贵但安全边际一般")
        else:
            broken.append("低估值逻辑未被当前PE/PB支撑")

    if any(k in logic for k in ["地产", "物业"]):
        flags.append("地产链仍是主要折价和风险来源")

    if any(k in logic for k in ["新药", "可灵", "AI硬件", "期权"]):
        flags.append("事件/产品期权逻辑，财务数据验证不足")

    if any(k in logic for k in ["出海", "美元", "代工", "国际"]):
        if profit_trend in {"增长", "稳定"}:
            ok.append("盈利趋势未明显破坏出海/代工逻辑")
        else:
            flags.append("出海/代工逻辑需结合订单和汇率二次跟踪")

    if any(k in logic for k in ["电力紧张", "电解铝", "铜金", "铝价"]):
        flags.append("资源/商品价格假设，需独立跟踪周期拐点")

    if "B/A负溢价" in logic or "MSCI被动卖出" in logic or "B/A折价" in theme:
        discount = to_float(row.get("ba_discount"))
        if pd.notna(discount) and discount > 0.15:
            ok.append("B/A折价仍较明显")
        elif pd.notna(discount):
            flags.append("B/A折价已收窄，需要重算吸引力")
        else:
            flags.append("B/A折价需人工复核")

    if risk_ann_count > 0:
        flags.append("近半年公告标题存在风险关键词")
    if profit_trend in {"明显下滑", "高点回落", "亏损/转亏"}:
        flags.append(f"利润趋势为{profit_trend}")
    if pd.notna(profit_yoy) and profit_yoy < -0.2:
        flags.append("最新净利同比下滑超过20%")
    if pd.notna(cfo_profit) and cfo_profit < 0.6:
        flags.append("经营现金流/利润偏低")

    if broken:
        status = "需复核"
    elif flags:
        status = "未破但需跟踪"
    else:
        status = "暂未打破"
    reason = "；".join(dict.fromkeys(broken + flags + ok)) or "暂无明显反证"
    return status, reason, "；".join(dict.fromkeys(ok))


def risk_grade(row: dict[str, Any]) -> tuple[str, str]:
    score = 0
    reasons: list[str] = []
    if str(row.get("logic_status")) == "需复核":
        score += 2
        reasons.append("买入逻辑需复核")
    if to_float(row.get("position_weight")) > 0.08 and any(k in str(row.get("theme")) for k in ["地产", "创新药", "AI硬件", "特殊高息", "资源周期"]):
        score += 2
        reasons.append("高波动/事件型仓位偏重")
    if to_float(row.get("risk_announcement_count")) > 0:
        score += 1
        reasons.append("公告风险关键词")
    if str(row.get("profit_trend")) in {"明显下滑", "高点回落", "亏损/转亏"}:
        score += 1
        reasons.append("利润趋势压力")
    div_yield = to_float(row.get("dividend_yield"))
    shareholder_yield = to_float(row.get("shareholder_return_yield"))
    return_yield = np.nanmax([div_yield, shareholder_yield]) if not (pd.isna(div_yield) and pd.isna(shareholder_yield)) else np.nan
    if pd.notna(return_yield) and return_yield < 0.02 and any(k in str(row.get("买入逻辑")) for k in ["分红", "股息", "股东回报"]):
        score += 1
        reasons.append("分红证据不足")
    if score >= 4:
        return "高", "；".join(reasons)
    if score >= 2:
        return "中", "；".join(reasons)
    return "低", "；".join(reasons) or "暂无高优先级风险"


def position_comment(row: dict[str, Any]) -> str:
    w = to_float(row.get("position_weight"))
    status = str(row.get("logic_status"))
    risk = str(row.get("risk_level"))
    if pd.isna(w):
        return "仓位无法计算"
    if w >= 0.12:
        bucket = "过重"
    elif w >= 0.08:
        bucket = "偏重"
    elif w >= 0.04:
        bucket = "中等"
    elif w >= 0.01:
        bucket = "小仓"
    else:
        bucket = "观察仓"
    if risk == "高" and w >= 0.04:
        return f"{bucket}，风险不匹配，建议降权/复核"
    if status == "需复核" and w >= 0.03:
        return f"{bucket}，逻辑待复核前不宜加仓"
    if bucket in {"过重", "偏重"}:
        return f"{bucket}，需确认组合集中度"
    return f"{bucket}，仓位大体匹配"


def make_decision(row: dict[str, Any]) -> str:
    status = str(row.get("logic_status"))
    risk = str(row.get("risk_level"))
    w = to_float(row.get("position_weight"))
    if status == "需复核" or risk == "高":
        return "优先复核/降权候选"
    if risk == "中":
        return "持有观察，暂停加仓"
    if pd.notna(w) and w < 0.015 and status == "暂未打破":
        return "小仓可继续跟踪"
    return "可继续持有"


def compute_ba_discount(row: dict[str, Any], quote_map: dict[str, dict], fx: dict[str, float]) -> float:
    code = row.get("std_code")
    if code == "900926.SH":
        a_code = "600845.SH"
    elif code == "200596.SZ":
        a_code = "000596.SZ"
    else:
        return np.nan
    b_quote = quote_map.get(code, {})
    a_quote = quote_map.get(a_code, {})
    b_price = to_float(b_quote.get("price"))
    a_price = to_float(a_quote.get("price"))
    currency = row.get("quote_currency")
    if pd.isna(b_price) or pd.isna(a_price) or not a_price:
        return np.nan
    b_cny = b_price * fx.get(str(currency), 1.0)
    return 1.0 - b_cny / a_price


def build() -> tuple[pd.DataFrame, dict[str, Any]]:
    pro = ts.pro_api()
    holdings = pd.read_csv(OUT / "holdings_2026_06_30.csv")
    fx, fx_date = fetch_fx()
    latest_cn_day = latest_open_day(pro, "SSE", datetime.now(ASIA).strftime("%Y%m%d"))
    # Tushare may know that today is open before end-of-day data is ready.
    for _ in range(5):
        db = pro.daily_basic(
            trade_date=latest_cn_day,
            fields="ts_code,trade_date,close,pe_ttm,pb,total_mv,circ_mv,dv_ttm,dv_ratio,turnover_rate",
        )
        if not db.empty:
            break
        latest_cn_day = (pd.Timestamp(latest_cn_day) - pd.Timedelta(days=1)).strftime("%Y%m%d")
    daily_basic = db.set_index("ts_code") if not db.empty else pd.DataFrame()

    sina_codes = [i.std_code for i in INSTRUMENTS.values() if i.quote_source == "sina"]
    # Add A-share counterparts for B/A discount checks.
    sina_codes.extend(["600845.SH", "000596.SZ"])
    sina = fetch_sina_quotes(sorted(set(sina_codes)))
    tencent_codes = [i.std_code for i in INSTRUMENTS.values() if i.quote_source == "tencent"]
    tencent = fetch_tencent_quotes(tencent_codes)
    yahoo_quotes = [fetch_yahoo_quote(i.std_code) for i in INSTRUMENTS.values() if i.quote_source == "yahoo"]
    quote_map: dict[str, dict] = {}
    for df in [sina, tencent, pd.DataFrame([q for q in yahoo_quotes if q])]:
        if not df.empty:
            for _, r in df.iterrows():
                quote_map[str(r["ts_code"])] = r.to_dict()

    etf_rank = pd.read_csv(OUT / "a_dividend_etf_rank.csv") if (OUT / "a_dividend_etf_rank.csv").exists() else pd.DataFrame()
    etf_rank = etf_rank.set_index("ts_code") if not etf_rank.empty else pd.DataFrame()
    hk_stock_map = fetch_hkex_stock_map()

    rows: list[dict[str, Any]] = []
    for _, base in holdings.iterrows():
        inst = INSTRUMENTS[int(base["序号"])]
        quote = quote_map.get(inst.std_code, {})
        price = to_float(quote.get("price"))
        pnl = parse_pct(base["浮盈亏"])
        cost_price = price / (1.0 + pnl) if pd.notna(pnl) and (1.0 + pnl) != 0 and pd.notna(price) else np.nan
        qty = to_float(base["数量"])
        mv_quote = price * qty if pd.notna(price) and pd.notna(qty) else np.nan
        cost_quote = cost_price * qty if pd.notna(cost_price) and pd.notna(qty) else np.nan
        fx_rate = fx.get(inst.quote_currency, 1.0)
        row = {
            **base.to_dict(),
            "std_code": inst.std_code,
            "official_name": inst.official_name,
            "standard_market": inst.market,
            "asset_class": inst.asset_class,
            "theme": inst.theme,
            "quote_currency": inst.quote_currency,
            "quote_price": price,
            "quote_time": quote.get("quote_time", ""),
            "quote_source": quote.get("source", inst.quote_source),
            "quantity": qty,
            "pnl_pct": pnl,
            "inferred_cost_price": cost_price,
            "market_value_quote": mv_quote,
            "cost_value_quote": cost_quote,
            "market_value_cny": mv_quote * fx_rate if pd.notna(mv_quote) else np.nan,
            "cost_value_cny": cost_quote * fx_rate if pd.notna(cost_quote) else np.nan,
            "fx_cny_per_quote": fx_rate,
            "instrument_note": inst.notes,
            "ba_discount": np.nan,
        }
        row["ba_discount"] = compute_ba_discount(row, quote_map, fx)

        metrics: dict[str, Any] = {}
        if inst.asset_class in {"A股", "B股"} and inst.financial_code:
            fin = fetch_cn_bundle(pro, inst.financial_code)
            db_row = daily_basic.loc[inst.financial_code] if inst.financial_code in daily_basic.index else pd.Series(dtype=float)
            metrics = cn_financial_metrics(fin, db_row, price, inst.quote_currency, fx)
            if inst.asset_class == "B股":
                # PE/PB from the A-share line is not directly comparable to the B
                # share price. Keep it as company-level valuation context only.
                metrics["valuation_note"] = "PE/PB取A股公司口径；B股吸引力另看B/A折价"
        elif inst.asset_class == "港股" and inst.financial_code:
            fin = fetch_hk_bundle(pro, inst.financial_code)
            metrics = hk_financial_metrics(fin, price)
            if inst.std_code in quote_map:
                metrics["pe_ttm"] = quote_map[inst.std_code].get("pe_ttm", np.nan)
                metrics["pb"] = quote_map[inst.std_code].get("pb", np.nan)
                metrics["total_mv_quote"] = quote_map[inst.std_code].get("total_mv", np.nan)
            titles = fetch_hkex_titles(inst.std_code, hk_stock_map)
            if not titles.empty:
                titles = titles.sort_values("date_time", ascending=False)
                recent = [f"{r['date_time']}: {compact(r['title'])}" for _, r in titles.head(3).iterrows()]
                risk_titles = [f"{r['date_time']}: {compact(r['title'])}" for _, r in titles[titles["is_risk"] == True].head(3).iterrows()]
                metrics["recent_announcements"] = " || ".join(recent)
                metrics["risk_announcements"] = " || ".join(risk_titles)
                metrics["risk_announcement_count"] = len(titles[titles["is_risk"] == True])
            else:
                metrics["recent_announcements"] = ""
                metrics["risk_announcements"] = ""
                metrics["risk_announcement_count"] = 0
        elif inst.asset_class == "ETF":
            if inst.std_code in etf_rank.index:
                er = etf_rank.loc[inst.std_code]
                metrics = {
                    "report_date": er.get("nav_date", ""),
                    "latest_report_date": er.get("nav_date", ""),
                    "dividend_yield": to_float(er.get("dividend_yield_ttm")),
                    "dividend_cash_ttm": to_float(er.get("div_cash_ttm")),
                    "pe_ttm": np.nan,
                    "pb": np.nan,
                    "revenue_trend": "ETF不适用",
                    "profit_trend": "ETF不适用",
                    "yield_pctile_all": to_float(er.get("yield_pctile_all")),
                    "yield_pctile_1y": to_float(er.get("yield_pctile_1y")),
                    "yield_pctile_2y": to_float(er.get("yield_pctile_2y")),
                    "yield_valuation_hint": er.get("yield_valuation_hint", ""),
                    "recent_announcements": "",
                    "risk_announcements": "",
                    "risk_announcement_count": 0,
                    "financial_coverage": "Tushare fund_div/fund_daily + Sina行情",
                }
        elif inst.asset_class == "新加坡股":
            div_cash = to_float(quote.get("div_cash_ttm"))
            metrics = {
                "dividend_yield": div_cash / price if pd.notna(div_cash) and pd.notna(price) and price else np.nan,
                "dividend_cash_ttm": div_cash,
                "revenue_trend": "Tushare不覆盖",
                "profit_trend": "Tushare不覆盖",
                "recent_announcements": "",
                "risk_announcements": "",
                "risk_announcement_count": 0,
                "financial_coverage": "Yahoo行情/分红事件；需SGX财报补验",
            }
        row.update(metrics)
        total_mv_quote = to_float(row.get("total_mv_quote"))
        if pd.notna(total_mv_quote) and total_mv_quote > 0:
            shareholder_return = np.nansum([to_float(row.get("div_paid_latest")), to_float(row.get("buyback_latest"))])
            if shareholder_return > 0:
                row["shareholder_return_yield"] = shareholder_return / total_mv_quote
                if pd.isna(to_float(row.get("dividend_yield"))):
                    row["dividend_yield"] = row["shareholder_return_yield"]
                    row["dividend_yield_note"] = "以现金流表派息/回购除以当前总市值估算"
        if "shareholder_return_yield" not in row:
            row["shareholder_return_yield"] = row.get("dividend_yield", np.nan)
        logic_status, logic_reason, logic_support = logic_review(row)
        row["logic_status"] = logic_status
        row["logic_reason"] = logic_reason
        row["logic_support"] = logic_support
        rows.append(row)
        time.sleep(0.03)

    df = pd.DataFrame(rows)
    total_mv_cny = pd.to_numeric(df["market_value_cny"], errors="coerce").sum()
    df["position_weight"] = pd.to_numeric(df["market_value_cny"], errors="coerce") / total_mv_cny if total_mv_cny else np.nan
    for i, r in df.iterrows():
        rg, rr = risk_grade(r.to_dict())
        df.at[i, "risk_level"] = rg
        df.at[i, "risk_reason"] = rr
    for i, r in df.iterrows():
        df.at[i, "position_comment"] = position_comment(r.to_dict())
        df.at[i, "decision"] = make_decision(r.to_dict())

    # Decision rank: most important/risky first, then largest positions.
    risk_order = {"高": 0, "中": 1, "低": 2}
    logic_order = {"需复核": 0, "未破但需跟踪": 1, "暂未打破": 2}
    df["_risk_sort"] = df["risk_level"].map(risk_order).fillna(9)
    df["_logic_sort"] = df["logic_status"].map(logic_order).fillna(9)
    df = df.sort_values(["_risk_sort", "_logic_sort", "position_weight"], ascending=[True, True, False]).drop(columns=["_risk_sort", "_logic_sort"])

    meta = {
        "generated_at": datetime.now(ASIA).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "asof": ASOF,
        "latest_cn_trade_date": latest_cn_day,
        "fx_date": fx_date,
        "fx": fx,
        "total_mv_cny": total_mv_cny,
        "quote_times": sorted({str(x) for x in df["quote_time"].dropna().tolist() if str(x)}),
    }
    return df, meta


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].fillna("")
    return out.replace([np.inf, -np.inf], np.nan)


def render_markdown(df: pd.DataFrame, meta: dict[str, Any]) -> str:
    summary_cols = [
        "std_code",
        "official_name",
        "theme",
        "position_weight",
        "quote_price",
        "inferred_cost_price",
        "dividend_yield",
        "pe_ttm",
        "pb",
        "profit_trend",
        "logic_status",
        "risk_level",
        "decision",
    ]
    view = df[summary_cols].copy()
    for c in ["position_weight", "dividend_yield"]:
        view[c] = view[c].map(lambda x: pct_text(x))
    for c in ["quote_price", "inferred_cost_price", "pe_ttm", "pb"]:
        view[c] = view[c].map(lambda x: num_text(x))
    theme = (
        df.groupby("theme", dropna=False)["market_value_cny"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
        .assign(weight=lambda x: x["market_value_cny"] / meta["total_mv_cny"])
    )
    theme["weight"] = theme["weight"].map(lambda x: pct_text(x))
    theme["market_value_cny"] = theme["market_value_cny"].map(lambda x: f"{x/10000:.1f}万")

    def markdown_table(data: pd.DataFrame) -> str:
        cols = list(data.columns)
        lines = [
            "| " + " | ".join(str(c) for c in cols) + " |",
            "| " + " | ".join("---" for _ in cols) + " |",
        ]
        for _, row in data.iterrows():
            vals = [str(row.get(c, "")).replace("\n", " ").replace("|", "\\|") for c in cols]
            lines.append("| " + " | ".join(vals) + " |")
        return "\n".join(lines)

    return "\n".join(
        [
            "# 持仓体检与买入逻辑复核",
            "",
            f"- 生成时间：{meta['generated_at']}",
            f"- 行情日：Tushare A股/ETF日线 {meta['latest_cn_trade_date']}；Sina/Tencent 实时报价实际返回时间见 CSV。",
            f"- 汇率：Frankfurter {meta['fx_date']}，用于跨币种仓位估算。",
            "- 成本价：按 `当前价 / (1 + 手写浮盈亏%)` 倒推，未计交易费、分红再投资和多次买卖影响。",
            "- 结论标签不是投资建议，只是把现有买入逻辑和可得数据做一致性检查。",
            "",
            "## 主题仓位",
            "",
            markdown_table(theme),
            "",
            "## 优先复核清单",
            "",
            markdown_table(view),
            "",
        ]
    )


def render_html(df: pd.DataFrame, meta: dict[str, Any]) -> str:
    total = meta["total_mv_cny"]
    risk_counts = df["risk_level"].value_counts().to_dict()
    logic_counts = df["logic_status"].value_counts().to_dict()
    theme = (
        df.groupby("theme", dropna=False)["market_value_cny"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
        .assign(weight=lambda x: x["market_value_cny"] / total)
        .head(12)
    )
    theme_rows = "\n".join(
        f"<tr><td>{esc(r['theme'])}</td><td>{r['market_value_cny']/10000:.1f}万</td><td>{pct_text(r['weight'])}</td></tr>"
        for _, r in theme.iterrows()
    )
    cards = []
    for _, r in df.iterrows():
        risk_cls = {"高": "bad", "中": "warn", "低": "good"}.get(str(r.get("risk_level")), "")
        logic_cls = {"需复核": "bad", "未破但需跟踪": "warn", "暂未打破": "good"}.get(str(r.get("logic_status")), "")
        recent = [x for x in str(r.get("recent_announcements") or "").split(" || ") if x][:3]
        recent_html = "".join(f"<li>{esc(x)}</li>" for x in recent) or "<li>暂无抓取到近期公告标题</li>"
        cards.append(
            f"""
    <article class="card">
      <div class="head">
        <div><b>{esc(r['official_name'])}</b><span>{esc(r['std_code'])}</span></div>
        <div class="pill {risk_cls}">风险 {esc(r.get('risk_level'))}</div>
      </div>
      <div class="grid">
        <div><label>仓位</label><strong>{pct_text(r.get('position_weight'))}</strong><small>{esc(r.get('position_comment'))}</small></div>
        <div><label>现价/成本</label><strong>{money_text(r.get('quote_price'), r.get('quote_currency'))} / {money_text(r.get('inferred_cost_price'), r.get('quote_currency'))}</strong><small>{esc(r.get('quote_time'))}</small></div>
        <div><label>股息率</label><strong>{pct_text(r.get('dividend_yield'))}</strong><small>TTM/最新可得口径</small></div>
        <div><label>PE/PB</label><strong>{num_text(r.get('pe_ttm'))} / {num_text(r.get('pb'))}</strong><small>{esc(r.get('valuation_note'))}</small></div>
        <div><label>盈利趋势</label><strong>{esc(r.get('profit_trend'))}</strong><small>利润CAGR {pct_text(r.get('profit_cagr'))}</small></div>
        <div><label>现金流</label><strong>{pct_text(r.get('cfo_profit'))}</strong><small>CFO/利润</small></div>
      </div>
      <div class="logic"><span class="{logic_cls}">{esc(r.get('logic_status'))}</span>：{esc(r.get('logic_reason'))}</div>
      <div class="decision">{esc(r.get('decision'))}</div>
      <details>
        <summary>公告与数据口径</summary>
        <ul>{recent_html}</ul>
        <p>{esc(r.get('financial_coverage'))}；{esc(r.get('instrument_note'))}</p>
      </details>
    </article>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>持仓体检与买入逻辑复核</title>
  <style>
    :root{{--bg:#f5f6f8;--fg:#17202a;--muted:#647080;--card:#fff;--line:#dde3ea;--blue:#155e75;--good:#16734a;--warn:#9a6500;--bad:#a32018}}
    *{{box-sizing:border-box}}
    body{{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Arial,sans-serif}}
    header{{position:sticky;top:0;z-index:3;background:rgba(245,246,248,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(10px)}}
    .bar,main{{max-width:1240px;margin:0 auto;padding:14px}}
    h1{{font-size:20px;line-height:1.25;margin:0 0 6px}}
    .meta{{font-size:13px;color:var(--muted);line-height:1.5}}
    .links{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}}
    .links a{{background:var(--blue);color:white;text-decoration:none;border-radius:6px;padding:8px 10px;font-size:14px}}
    .summary{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:12px}}
    .metric,.panel,.card{{background:var(--card);border:1px solid var(--line);border-radius:8px}}
    .metric{{padding:10px}}
    .metric label,.grid label{{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}}
    .metric strong{{font-size:18px}}
    .note{{background:#fff8e6;border:1px solid #ead498;color:#3c2a00;border-radius:8px;padding:10px;font-size:13px;line-height:1.55;margin-bottom:12px}}
    .panel{{padding:10px;margin-bottom:12px;overflow:auto}}
    table{{width:100%;border-collapse:collapse;font-size:13px}}
    th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:left;white-space:nowrap}}
    .cards{{display:grid;grid-template-columns:1fr;gap:10px}}
    .card{{padding:12px}}
    .head{{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;margin-bottom:10px}}
    .head b{{display:block;font-size:16px}}
    .head span{{display:block;color:var(--muted);font-size:12px;margin-top:3px}}
    .pill{{border-radius:999px;padding:4px 8px;font-size:12px;font-weight:700;white-space:nowrap}}
    .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:10px}}
    .grid div{{border:1px solid var(--line);border-radius:6px;padding:8px;min-height:72px}}
    .grid strong{{display:block;font-size:15px;line-height:1.2}}
    .grid small{{display:block;color:var(--muted);font-size:11px;line-height:1.35;margin-top:4px}}
    .logic{{font-size:13px;line-height:1.55;border-top:1px solid var(--line);padding-top:9px}}
    .decision{{margin-top:8px;font-size:13px;font-weight:700}}
    details{{margin-top:8px;color:var(--muted);font-size:12px;line-height:1.45}}
    details summary{{cursor:pointer;color:var(--fg)}}
    .good{{color:var(--good);background:#eaf7f0}}
    .warn{{color:var(--warn);background:#fff4da}}
    .bad{{color:var(--bad);background:#fdecea}}
    .logic .good,.logic .warn,.logic .bad{{background:transparent;font-weight:700}}
    @media(min-width:760px){{.summary{{grid-template-columns:repeat(4,minmax(0,1fr))}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}.grid{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>持仓体检与买入逻辑复核</h1>
      <div class="meta">生成时间：{esc(meta['generated_at'])}。行情以 Sina/Tencent/Yahoo 当前返回值和 Tushare {esc(meta['latest_cn_trade_date'])} 日线为准；跨币种仓位用 Frankfurter {esc(meta['fx_date'])} 汇率估算。</div>
    </div>
  </header>
  <main>
    <nav class="links">
      <a href="index.html">返回主榜</a>
      <a href="holdings_2026_06_30.html">原始持仓表</a>
      <a href="holdings_health_check_2026_07_01.csv">CSV</a>
      <a href="holdings_health_check_2026_07_01.md">Markdown</a>
    </nav>
    <section class="summary">
      <div class="metric"><label>估算总市值</label><strong>{total/10000:.1f}万 CNY</strong></div>
      <div class="metric"><label>高风险</label><strong>{risk_counts.get('高',0)} 个</strong></div>
      <div class="metric"><label>中风险</label><strong>{risk_counts.get('中',0)} 个</strong></div>
      <div class="metric"><label>需复核逻辑</label><strong>{logic_counts.get('需复核',0)} 个</strong></div>
    </section>
    <div class="note">成本价按当前价和手写浮盈亏倒推，不能替代真实成交记录。对港股/新加坡股，Tushare 财报字段存在滞后和口径差异；事件驱动逻辑需要继续用公告、年报和行业数据二次验证。</div>
    <section class="panel">
      <table>
        <thead><tr><th>主题</th><th>市值(CNY)</th><th>权重</th></tr></thead>
        <tbody>{theme_rows}</tbody>
      </table>
    </section>
    <section class="cards">
      {''.join(cards)}
    </section>
  </main>
</body>
</html>
"""


def main() -> None:
    df, meta = build()
    csv_path = OUT / "holdings_health_check_2026_07_01.csv"
    md_path = OUT / "holdings_health_check_2026_07_01.md"
    html_path = OUT / "holdings_health_check_2026_07_01.html"
    latest_path = OUT / "holdings_health_check.html"
    csv_safe(df).to_csv(csv_path, index=False, quoting=csv.QUOTE_MINIMAL)
    md_path.write_text(render_markdown(df, meta), encoding="utf-8")
    html_text = render_html(df, meta)
    html_path.write_text(html_text, encoding="utf-8")
    latest_path.write_text(html_text, encoding="utf-8")
    meta_path = OUT / "holdings_health_check_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(csv_path)
    print(md_path)
    print(html_path)
    print(f"rows={len(df)} total_mv_cny={meta['total_mv_cny']:.2f}")
    print(df[["std_code", "official_name", "position_weight", "logic_status", "risk_level", "decision"]].head(12).to_string(index=False))


if __name__ == "__main__":
    main()
