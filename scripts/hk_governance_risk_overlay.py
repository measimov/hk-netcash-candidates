#!/usr/bin/env python3
"""
Governance / "old-thousand stock" risk overlay for the HK candidate pool.

Evidence hierarchy:
- Hard evidence: HKEX regulatory announcements and SFC enforcement news.
- Behavioural signals: issuer announcement title patterns from HKEXnews.

The behavioural layer is a warning system, not a finding of misconduct.
"""

from __future__ import annotations

import concurrent.futures as futures
import html
import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("HK_NETCASH_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve()
UA = {"User-Agent": "Mozilla/5.0"}
TODAY = "20260622"
ANN_FROM = "20210101"
REG_YEARS = range(2018, 2027)


HKEX_REG_URL = "https://www.hkex.com.hk/News/Regulatory-Announcements"
HKEX_REG_POST = "https://www.hkex.com.hk/layouts/HKEX_Common/Tab/NewsCentreDetailsLoad.aspx/DisplayNewsCentreDetailsLoad"
HKEXNEWS_ACTIVE = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
HKEXNEWS_TITLE = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
SFC_NEWS_SEARCH = "https://apps.sfc.hk/edistributionWeb/api/news/search"


def norm_space(s: str) -> str:
    return " ".join(str(s or "").replace("\xa0", " ").split())


def strip_html(s: str) -> str:
    return norm_space(re.sub(r"<[^>]+>", " ", html.unescape(str(s or ""))))


def code5(ts_code: str) -> str:
    return str(ts_code).split(".")[0].zfill(5)


def canonical_company_name(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]+", " ", str(name or "").upper())
    stop = {
        "LTD",
        "LIMITED",
        "CO",
        "COMPANY",
        "INC",
        "INCORPORATED",
        "HOLDINGS",
        "HOLDING",
        "GROUP",
        "PLC",
        "THE",
        "CORPORATION",
        "CORP",
    }
    toks = [t for t in s.split() if t and t not in stop]
    return " ".join(toks)


def parse_hkex_reg_html(fragment: str, year: int) -> list[dict]:
    soup = BeautifulSoup(fragment, "html.parser")
    rows = []
    for block in soup.select(".whats_on_tdy_row"):
        date_txt = norm_space(block.select_one(".whats_on_tdy_ball").get_text(" ", strip=True) if block.select_one(".whats_on_tdy_ball") else "")
        title_a = block.select_one(".whats_on_tdy_text_2 a")
        if not title_a:
            continue
        title = norm_space(title_a.get_text(" ", strip=True))
        href = title_a.get("href", "")
        if href.startswith("/"):
            href = "https://www.hkex.com.hk" + href
        codes = sorted(set(c.zfill(5) for c in re.findall(r"Stock Code\s*:?\s*(\d{1,5})", title, flags=re.I)))
        if not codes:
            codes = sorted(set(c.zfill(5) for c in re.findall(r"Previous Stock Code\s*:?\s*(\d{1,5})", title, flags=re.I)))
        kind = "other"
        low = title.lower()
        if "disciplinary action" in low or "censures" in low or "criticises" in low or "breaching the listing rules" in low:
            kind = "disciplinary"
        elif "cancellation of listing" in low:
            kind = "cancellation"
        elif "listing review" in low or "review committee" in low:
            kind = "listing_review"
        rows.append(
            {
                "year": year,
                "date_text": date_txt,
                "title": title,
                "url": href,
                "codes": ";".join(codes),
                "kind": kind,
            }
        )
    return rows


def fetch_hkex_regulatory_index(refresh: bool = False) -> pd.DataFrame:
    path = OUT / "hkex_regulatory_2018_2026.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path)

    all_rows = []
    session = requests.Session()
    session.headers.update(UA)
    for year in REG_YEARS:
        params = {
            "sc_lang": "en",
            "DateFrom": f"{year}-01-01",
            "DateTo": f"{year}-12-31",
            "Category": "undefined",
            "Category2": "undefined",
        }
        page = session.get(HKEX_REG_URL, params=params, timeout=25)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        all_rows.extend(parse_hkex_reg_html(page.text, year))
        hidden = {inp.get("id"): inp.get("value", "") for inp in soup.find_all("input", type="hidden") if inp.get("id")}
        current = int(hidden.get("currentLoadCount") or 20)
        load_more = int(hidden.get("LoadMoreCount") or 20)
        while True:
            payload = {
                "pageUrl": hidden.get("pageUrl", "/News/Regulatory-Announcements?sc_lang=en"),
                "TopicFieldName": hidden.get("TopicFieldName", "News Topic"),
                "DateFieldName": hidden.get("DateFieldName", "News Date"),
                "FilesFieldName": hidden.get("FilesFieldName", "News Files"),
                "ImageFieldName": hidden.get("ImageFieldName", "News Image"),
                "ContentFieldName": hidden.get("ContentFieldName", "News Description"),
                "Category1FieldName": hidden.get("Category1FieldName", "News Category1"),
                "Category2FieldName": hidden.get("Category2FieldName", "News Category2"),
                "Category3FieldName": hidden.get("Category3FieldName", "News Category3"),
                "currentcount": current,
                "loadmorecount": load_more,
                "IsLoadMore": True,
                "isCardView": hidden.get("isCardView", "N"),
                "TabItemSourceID": hidden.get("tabItemSourceID", "{0C3405ED-78E4-45C5-B743-62D502A15E77}"),
                "datefrom": f"{year}-01-01",
                "dateto": f"{year}-12-31",
                "category": "",
                "keyword": "",
                "isHideDay": hidden.get("isHideDay", "False"),
                "category2": "",
                "TargetLanguage": "en",
                "TargetSite": None,
                "host": "www.hkex.com.hk",
            }
            resp = session.post(HKEX_REG_POST, json=payload, timeout=25, headers={"Referer": page.url, **UA})
            resp.raise_for_status()
            frag = resp.json().get("d") or ""
            if not frag.strip():
                break
            rows = parse_hkex_reg_html(frag, year)
            all_rows.extend(rows)
            current += load_more
            time.sleep(0.05)
            if not rows:
                break
        print(f"HKEX regulatory {year}: cumulative {len(all_rows)}")
    df = pd.DataFrame(all_rows).drop_duplicates(["url", "title"])
    df.to_csv(path, index=False)
    return df


def fetch_sfc_enforcement(refresh: bool = False) -> pd.DataFrame:
    path = OUT / "sfc_enforcement_news.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path)
    session = requests.Session()
    session.headers.update(UA)
    rows = []
    page_no = 0
    page_size = 100
    total = None
    while total is None or page_no * page_size < total:
        payload = {
            "lang": "EN",
            "category": "enforcement",
            "year": "all",
            "month": "all",
            "pageNo": page_no,
            "pageSize": page_size,
            "isLoading": True,
            "errors": None,
            "items": None,
            "total": -1,
        }
        resp = session.post(SFC_NEWS_SEARCH, json=payload, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        total = int(data.get("total") or 0)
        items = data.get("items") or []
        for it in items:
            rows.append(
                {
                    "news_ref": it.get("newsRefNo"),
                    "issue_date": it.get("issueDate"),
                    "title": norm_space(it.get("title")),
                    "news_type": it.get("newsType"),
                    "target_ce": "; ".join(norm_space(x.get("ceName")) for x in it.get("targetCeList") or []),
                    "url": f"https://apps.sfc.hk/edistributionWeb/gateway/EN/news-and-announcements/news/doc?refNo={it.get('newsRefNo')}",
                }
            )
        print(f"SFC enforcement page {page_no + 1}: {len(rows)}/{total}")
        if not items:
            break
        page_no += 1
        time.sleep(0.05)
    df = pd.DataFrame(rows).drop_duplicates(["news_ref"])
    df.to_csv(path, index=False)
    return df


def fetch_active_stock_map(refresh: bool = False) -> dict[str, dict]:
    path = OUT / "hkexnews_active_stock_map.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path)
    else:
        data = requests.get(HKEXNEWS_ACTIVE, timeout=25, headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en", **UA}).json()
        df = pd.DataFrame(data)
        df["c"] = df["c"].astype(str).str.zfill(5)
        df.to_csv(path, index=False)
    return {str(r["c"]).zfill(5): r for _, r in df.iterrows()}


def fetch_company_titles(row: dict, stock_map: dict[str, dict]) -> list[dict]:
    c = code5(row["ts_code"])
    stock = stock_map.get(c)
    if stock is None:
        return []
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": str(stock["i"]),
        "documentType": "-1",
        "fromDate": ANN_FROM,
        "toDate": TODAY,
        "title": "",
        "searchType": "rbAfter2006",
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": "1000",
        "lang": "en",
    }
    resp = requests.get(
        HKEXNEWS_TITLE,
        params=params,
        timeout=30,
        headers={"Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en", **UA},
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("result")
    if not raw or raw == "null":
        return []
    items = json.loads(raw)
    rows = []
    for it in items:
        rows.append(
            {
                "ts_code": row["ts_code"],
                "name": row["name"],
                "stock_code": c,
                "date_time": it.get("DATE_TIME"),
                "stock_name": it.get("STOCK_NAME"),
                "title": strip_html(it.get("TITLE")),
                "long_text": strip_html(it.get("LONG_TEXT")),
                "short_text": strip_html(it.get("SHORT_TEXT")),
                "file_link": "https://www1.hkexnews.hk" + it.get("FILE_LINK", "") if it.get("FILE_LINK") else "",
            }
        )
    return rows


def fetch_candidate_titles(candidates: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    path = OUT / "hkex_candidate_titles_2021_2026.csv"
    if path.exists() and not refresh:
        existing = pd.read_csv(path)
        if "title" in existing.columns and not existing.get("error", pd.Series(dtype=str)).notna().any():
            return existing
        print("Existing HKEXnews title cache is incomplete; refreshing it.")
    stock_map = fetch_active_stock_map(refresh=refresh)
    rows: list[dict] = []
    tasks = candidates[["ts_code", "name"]].drop_duplicates("ts_code").to_dict("records")
    with futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_company_titles, r, stock_map): r for r in tasks}
        done = 0
        for fut in futures.as_completed(futs):
            done += 1
            r = futs[fut]
            try:
                rows.extend(fut.result())
            except Exception as exc:
                rows.append({"ts_code": r["ts_code"], "name": r["name"], "error": str(exc)})
            if done % 25 == 0 or done == len(tasks):
                print(f"HKEXnews titles: {done}/{len(tasks)}, rows {len(rows)}")
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return df


PATTERNS = {
    "dilution": re.compile(r"\b(placing|rights issue|open offer|subscription of new shares|issue of new shares|convertible bond|convertible note|warrant|specific mandate)\b", re.I),
    "capital_reorg": re.compile(r"\b(share consolidation|capital reorganisation|capital reorganization|capital reduction|subdivision of shares|change in board lot size)\b", re.I),
    "connected": re.compile(r"\b(connected transaction|continuing connected transaction)\b", re.I),
    "delay_suspend": re.compile(r"\b(delay in publication|further delay|suspension of trading|continued suspension|trading halt|resumption guidance|failure to publish|non-publication)\b", re.I),
    "audit": re.compile(r"\b(resignation of auditor|change of auditor|removal of auditor|qualified opinion|disclaimer of opinion|modified opinion|material uncertainty|going concern|unaudited annual results|delay in publication of audited)\b", re.I),
    "investigation": re.compile(r"\b(investigation|independent investigation|forensic|internal control|unauthorised|unauthorized|misappropriation|fraud|non-compliance|breach)\b", re.I),
    "winding": re.compile(r"\b(winding up|winding-up|petition|receivership|liquidation|bankruptcy)\b", re.I),
    "offer_privatisation": re.compile(r"\b(privatisation|privatization|scheme of arrangement|withdrawal of listing|mandatory unconditional cash offer|possible offer|general offer|offeror)\b", re.I),
    "director_auditor_change": re.compile(r"\b(resignation of director|resignation of independent non-executive director|resignation of auditor|change of auditor|removal of auditor)\b", re.I),
    "profit_warning": re.compile(r"\bprofit warning\b", re.I),
}


def score_patterns(titles: Iterable[str]) -> tuple[dict[str, int], int, list[str], list[str]]:
    counts: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {k: [] for k in PATTERNS}
    for title in titles:
        title = norm_space(title)
        for key, pat in PATTERNS.items():
            if pat.search(title):
                counts[key] += 1
                if len(evidence[key]) < 3:
                    evidence[key].append(title)

    score = 0
    notes = []
    if counts["dilution"]:
        score += min(counts["dilution"] * 6, 30)
        notes.append(f"稀释融资/配股供股 {counts['dilution']} 次")
    if counts["capital_reorg"]:
        score += min(counts["capital_reorg"] * 16, 35)
        notes.append(f"合股/股本重组 {counts['capital_reorg']} 次")
    if counts["dilution"] >= 2 and counts["capital_reorg"] >= 1:
        score += 25
        notes.append("配股/供股与合股组合，老千结构动作")
    if counts["connected"] >= 8:
        score += 18
        notes.append(f"关联交易频繁 {counts['connected']} 次")
    elif counts["connected"]:
        score += min(counts["connected"] * 2, 10)
        notes.append(f"关联交易 {counts['connected']} 次")
    if counts["delay_suspend"]:
        score += min(counts["delay_suspend"] * 12, 40)
        notes.append(f"延迟刊发/停复牌相关 {counts['delay_suspend']} 次")
    if counts["audit"]:
        score += min(counts["audit"] * 10, 35)
        notes.append(f"审计/核数师风险 {counts['audit']} 次")
    if counts["investigation"]:
        score += min(counts["investigation"] * 15, 45)
        notes.append(f"调查/内控/违规关键词 {counts['investigation']} 次")
    if counts["winding"]:
        score += min(counts["winding"] * 25, 60)
        notes.append(f"清盘/呈请/接管关键词 {counts['winding']} 次")
    if counts["offer_privatisation"]:
        score += min(counts["offer_privatisation"] * 8, 30)
        notes.append(f"要约/私有化/撤销上市关键词 {counts['offer_privatisation']} 次")
    if counts["director_auditor_change"] >= 4:
        score += 10
        notes.append(f"董事/核数师变动较多 {counts['director_auditor_change']} 次")
    if counts["profit_warning"]:
        score += min(counts["profit_warning"] * 2, 8)
        notes.append(f"盈利警告 {counts['profit_warning']} 次")

    snippets = []
    for key, vals in evidence.items():
        for v in vals[:2]:
            snippets.append(f"{key}: {v}")
    return dict(counts), score, notes, snippets[:8]


def match_sfc_hits(row: pd.Series, sfc: pd.DataFrame, basic_row: pd.Series | None) -> list[dict]:
    c = code5(row["ts_code"])
    c_nozero = str(int(c))
    aliases = [str(row["name"])]
    if basic_row is not None:
        aliases.extend([str(basic_row.get("fullname", "")), str(basic_row.get("enname", ""))])
    en_aliases = []
    for a in aliases:
        ca = canonical_company_name(a)
        if len(ca) >= 6 and len(ca.split()) >= 1:
            en_aliases.append(ca)
    hits = []
    for _, n in sfc.iterrows():
        title = str(n.get("title", ""))
        nt = canonical_company_name(title)
        title_low = title.lower()
        code_hit = re.search(rf"\b(stock code|code)\s*:?\s*0*{re.escape(c_nozero)}\b", title_low, flags=re.I)
        alias_hit = any(a and a in nt for a in en_aliases)
        if code_hit or alias_hit:
            hits.append({"title": title, "url": n.get("url", ""), "issue_date": n.get("issue_date", "")})
    return hits[:10]


def risk_grade(score: int, hard_hit: bool) -> str:
    if hard_hit or score >= 80:
        return "Red"
    if score >= 45:
        return "Amber"
    if score >= 20:
        return "Watch"
    return "Clean"


def parse_date(value) -> pd.Timestamp:
    if pd.isna(value):
        return pd.NaT
    s = str(value)
    if re.fullmatch(r"\d{8}", s):
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


def filter_hits_after_listing(hits: list[dict], list_date: pd.Timestamp, date_key: str) -> list[dict]:
    if pd.isna(list_date):
        return hits
    out = []
    for h in hits:
        d = parse_date(h.get(date_key))
        if pd.isna(d) or d >= list_date:
            out.append(h)
    return out


def main() -> None:
    candidates = pd.read_csv(OUT / "expanded_quality_candidates.csv")
    basic = pd.read_csv(OUT / "tushare_hk_basic.csv").set_index("ts_code")

    hkex_reg = fetch_hkex_regulatory_index()
    sfc = fetch_sfc_enforcement()
    titles = fetch_candidate_titles(candidates)
    if "title" not in titles.columns:
        titles["title"] = ""
    titles["title"] = titles["title"].fillna("").astype(str)

    reg_by_code: dict[str, list[dict]] = {}
    for _, r in hkex_reg.iterrows():
        for c in str(r.get("codes", "")).split(";"):
            c = c.strip().zfill(5)
            if c and c != "00000":
                reg_by_code.setdefault(c, []).append(r.to_dict())

    grouped_titles = titles.groupby("ts_code")["title"].apply(list).to_dict() if not titles.empty and "title" in titles else {}
    rows = []
    for _, row in candidates.iterrows():
        c = code5(row["ts_code"])
        basic_row = basic.loc[row["ts_code"]] if row["ts_code"] in basic.index else None
        list_date = parse_date(basic_row.get("list_date")) if basic_row is not None else pd.NaT
        reg_hits = filter_hits_after_listing(reg_by_code.get(c, []), list_date, "date_text")
        sfc_hits = filter_hits_after_listing(match_sfc_hits(row, sfc, basic_row), list_date, "issue_date")
        counts, behaviour_score, notes, snippets = score_patterns(grouped_titles.get(row["ts_code"], []))

        hard_score = 0
        hard_notes = []
        if reg_hits:
            hard_score += 70
            hard_notes.append(f"HKEX监管公告命中 {len(reg_hits)} 条")
        if sfc_hits:
            hard_score += 70
            hard_notes.append(f"SFC执法新闻命中 {len(sfc_hits)} 条")
        score = min(hard_score + behaviour_score, 100)
        grade = risk_grade(score, bool(reg_hits or sfc_hits))
        if grade == "Red":
            action = "剔除/只作案例复核"
        elif grade == "Amber":
            action = "降权，需逐条公告复核"
        elif grade == "Watch":
            action = "保留但加治理折价"
        else:
            action = "暂未命中明显治理红旗"

        rows.append(
            {
                **row.to_dict(),
                "governance_grade": grade,
                "governance_score": score,
                "governance_action": action,
                "governance_notes": "；".join(hard_notes + notes) or "未命中官方处分或高风险公告模式",
                "hkex_regulatory_hits": len(reg_hits),
                "sfc_enforcement_hits": len(sfc_hits),
                "dilution_count": counts.get("dilution", 0),
                "capital_reorg_count": counts.get("capital_reorg", 0),
                "connected_count": counts.get("connected", 0),
                "delay_suspend_count": counts.get("delay_suspend", 0),
                "audit_count": counts.get("audit", 0),
                "investigation_count": counts.get("investigation", 0),
                "winding_count": counts.get("winding", 0),
                "offer_privatisation_count": counts.get("offer_privatisation", 0),
                "director_auditor_change_count": counts.get("director_auditor_change", 0),
                "profit_warning_count": counts.get("profit_warning", 0),
                "evidence_titles": " || ".join(snippets),
                "hkex_regulatory_evidence": " || ".join(f"{r.get('date_text','')}: {r.get('title','')} {r.get('url','')}" for r in reg_hits[:5]),
                "sfc_evidence": " || ".join(f"{h.get('issue_date','')}: {h.get('title','')} {h.get('url','')}" for h in sfc_hits[:5]),
            }
        )

    out = pd.DataFrame(rows)
    grade_order = {"Clean": 0, "Watch": 1, "Amber": 2, "Red": 3}
    out["risk_order"] = out["governance_grade"].map(grade_order).fillna(9)
    out = out.sort_values(["risk_order", "tier", "quality_score"], ascending=[True, True, False]).drop(columns=["risk_order"])
    out.to_csv(OUT / "governance_risk_overlay.csv", index=False)

    # Practical research list: remove red flags, put clean names first.
    filtered = out.loc[~out["governance_grade"].eq("Red")].copy()
    tier_order = {"A": 0, "B+": 1, "B": 2, "Watch": 3}
    risk_penalty = {"Clean": 0, "Watch": 10, "Amber": 25, "Red": 100}
    filtered["final_score_after_governance"] = filtered["quality_score"] - filtered["governance_grade"].map(risk_penalty).fillna(0)
    filtered["tier_order"] = filtered["tier"].map(tier_order).fillna(9)
    filtered = filtered.sort_values(["tier_order", "final_score_after_governance"], ascending=[True, False]).drop(columns=["tier_order"])
    filtered.to_csv(OUT / "governance_filtered_candidates.csv", index=False)

    render_html(out, filtered)
    print(OUT / "governance_risk_overlay.csv")
    print(OUT / "governance_filtered_candidates.csv")
    print(OUT / "governance_risk_overlay.html")


def pct(x) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x) * 100:.1f}%"


def yi(x) -> str:
    if pd.isna(x):
        return ""
    return f"{float(x) / 1e8:.2f}"


def render_html(out: pd.DataFrame, filtered: pd.DataFrame) -> None:
    counts = out["governance_grade"].value_counts().to_dict()
    show = filtered.head(120).copy()
    cards = []
    for _, r in show.iterrows():
        cls = str(r["governance_grade"]).lower()
        cards.append(
            f"""
<article class="card {cls}">
  <div class="head"><b>{r['governance_grade']}</b><span>{r['tier']} {r['ts_code']} {r['name']}</span><em>{float(r['final_score_after_governance']):.1f}</em></div>
  <div class="meta">{r['governance_action']} · {r['governance_notes']}</div>
  <div class="grid">
    <div><label>质量分</label><strong>{float(r['quality_score']):.1f}</strong></div>
    <div><label>治理风险分</label><strong>{int(r['governance_score'])}</strong></div>
    <div><label>现金/负债</label><strong>{pct(r['cash_to_liab'])}</strong></div>
    <div><label>股东回报</label><strong>{pct(r['shareholder_return_yield'])}</strong></div>
    <div><label>稀释/合股</label><strong>{int(r['dilution_count'])}/{int(r['capital_reorg_count'])}</strong></div>
    <div><label>关联/要约</label><strong>{int(r['connected_count'])}/{int(r['offer_privatisation_count'])}</strong></div>
  </div>
  <p>{html.escape(str(r['evidence_titles'])[:700])}</p>
</article>"""
        )
    red = out.loc[out["governance_grade"].eq("Red")].copy()
    red_rows = []
    for _, r in red.head(80).iterrows():
        red_rows.append(
            f"<tr><td>{r['ts_code']}</td><td>{html.escape(str(r['name']))}</td><td>{r['tier']}</td><td>{int(r['governance_score'])}</td><td>{html.escape(str(r['governance_notes']))}</td><td>{html.escape(str(r['hkex_regulatory_evidence'] or r['sfc_evidence'])[:500])}</td></tr>"
        )
    html_doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>治理风险过滤层</title><style>
body{{margin:0;background:#f7f8fa;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',Arial,sans-serif}}header{{position:sticky;top:0;background:rgba(247,248,250,.94);backdrop-filter:blur(10px);border-bottom:1px solid #dbe1e8;padding:12px 14px}}h1{{font-size:20px;margin:0 0 4px}}.sub{{font-size:13px;color:#637083;line-height:1.45}}main{{max-width:1180px;margin:0 auto;padding:14px}}.links{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 16px}}.links a{{background:#176b87;color:white;text-decoration:none;padding:8px 10px;border-radius:6px;font-size:14px}}.summary{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:10px 0}}.summary div{{background:white;border:1px solid #dbe1e8;border-radius:8px;padding:10px}}.summary b{{display:block;font-size:20px}}.card{{background:white;border:1px solid #dbe1e8;border-left-width:5px;border-radius:8px;padding:12px;margin:10px 0}}.clean{{border-left-color:#247a4d}}.watch{{border-left-color:#b77a1a}}.amber{{border-left-color:#d06b21}}.red{{border-left-color:#a73838}}.head{{display:flex;gap:8px;align-items:center}}.head b{{font-size:18px;min-width:58px}}.head span{{font-weight:700;flex:1}}.head em{{font-style:normal;color:#176b87;font-weight:700}}.meta,p{{font-size:13px;color:#3d4a5c;line-height:1.5}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}}.grid div{{background:#f3f6f8;border-radius:6px;padding:8px}}label{{display:block;color:#637083;font-size:12px;margin-bottom:3px}}strong{{font-size:14px}}.tablewrap{{overflow:auto;background:white;border:1px solid #dbe1e8;border-radius:8px;margin-top:18px}}table{{width:100%;min-width:980px;border-collapse:collapse;font-size:13px}}td,th{{padding:8px;border-bottom:1px solid #e4e9ef;text-align:left;vertical-align:top}}@media(min-width:820px){{.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card{{margin:0}}.grid{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}</style></head>
<body><header><h1>治理风险过滤层</h1><div class="sub">官方监管处分为硬红旗；公告行为模式只作预警。覆盖 HKEX 监管公告 2018-2026、SFC enforcement news、披露易公司公告标题 2021-01-01 至 2026-06-22。</div></header><main>
<div class="links"><a href="index.html">主榜</a><a href="expanded_quality_candidates.html">宽口径合并榜</a><a href="governance_filtered_candidates.csv">过滤后CSV</a><a href="governance_risk_overlay.csv">治理明细CSV</a><a href="hkex_candidate_titles_2021_2026.csv">披露易标题CSV</a></div>
<section class="summary"><div><b>{len(out)}</b><span>总候选</span></div><div><b>{counts.get('Clean',0)}</b><span>Clean</span></div><div><b>{counts.get('Watch',0)+counts.get('Amber',0)}</b><span>Watch/Amber</span></div><div><b>{counts.get('Red',0)}</b><span>Red 剔除</span></div></section>
<section class="cards">{''.join(cards)}</section>
<h2>Red 硬红旗</h2><div class="tablewrap"><table><thead><tr><th>代码</th><th>名称</th><th>原档</th><th>风险分</th><th>原因</th><th>证据</th></tr></thead><tbody>{''.join(red_rows)}</tbody></table></div>
</main></body></html>"""
    (OUT / "governance_risk_overlay.html").write_text(html_doc, encoding="utf-8")


if __name__ == "__main__":
    main()
