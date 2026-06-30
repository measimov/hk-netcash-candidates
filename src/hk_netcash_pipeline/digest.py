from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .llm import DpskChatClient
from .stages import PipelineContext


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _top_records(df: pd.DataFrame, columns: list[str], limit: int) -> list[dict]:
    if df.empty:
        return []
    cols = [c for c in columns if c in df.columns]
    return df.head(limit)[cols].where(pd.notna(df), None).to_dict(orient="records")


def build_payload(output_dir: Path) -> dict:
    ranked = _read_csv(output_dir / "hk_ranked_candidates.csv")
    secondary = _read_csv(output_dir / "secondary_validation_top20.csv")
    governance = _read_csv(output_dir / "governance_filtered_candidates.csv")
    property_review = _read_csv(output_dir / "property_service_target_review.csv")
    dividend_etf = _read_csv(output_dir / "a_dividend_etf_rank.csv")
    special_dividend = _read_csv(output_dir / "special_dividend_watchlist.csv")
    quote_times = ranked.get("quote_time", pd.Series(dtype=str)).dropna().astype(str)
    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "quote_time_min": quote_times.min() if len(quote_times) else "",
        "quote_time_max": quote_times.max() if len(quote_times) else "",
        "primary_count": int(len(ranked)),
        "governance_filtered_count": int(len(governance)),
        "top_primary": _top_records(
            ranked,
            [
                "ts_code",
                "name",
                "price_hkd",
                "total_mv_hkd",
                "cash_to_liab",
                "net_cash_to_mv",
                "profit_latest",
                "cfo_to_profit_latest",
                "oneoff_ratio_latest",
                "dividend_paid_yield_est",
                "score",
                "liquidity_bucket",
                "flags",
            ],
            15,
        ),
        "secondary_top20": _top_records(
            secondary,
            [
                "secondary_grade",
                "source_rank",
                "ts_code",
                "name",
                "profit_trend",
                "cfo_profit_avg4",
                "oneoff_latest",
                "shareholder_return_yield_est",
                "secondary_concern",
            ],
            20,
        ),
        "governance_top": _top_records(
            governance,
            ["governance_grade", "ts_code", "name", "governance_score", "key_risks", "score"],
            20,
        ),
        "property_targets": _top_records(
            property_review,
            ["ts_code", "name", "verdict", "candidate_tier", "cash_to_liab", "net_cash_to_mv", "shareholder_return_yield_est", "comment"],
            10,
        ),
        "a_dividend_etf_top": _top_records(
            dividend_etf,
            [
                "rank",
                "ts_code",
                "name",
                "strategy_tag",
                "price",
                "dividend_yield_ttm",
                "div_cash_ttm",
                "amount_cny",
                "total_fee_rate",
                "premium_rate",
                "div_years_3y",
                "score",
            ],
            15,
        ),
        "special_dividend_top": _top_records(
            special_dividend,
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
            ],
            12,
        ),
    }


def deterministic_summary(payload: dict) -> str:
    top = payload.get("top_primary", [])[:5]
    secondary = payload.get("secondary_top20", [])
    etfs = payload.get("a_dividend_etf_top", [])[:5]
    special = payload.get("special_dividend_top", [])[:5]
    a_names = [f"{r.get('ts_code')} {r.get('name')}" for r in secondary if r.get("secondary_grade") == "A"]
    lines = [
        "# LLM/规则汇总",
        "",
        f"- 主榜候选: {payload.get('primary_count')}；治理过滤后: {payload.get('governance_filtered_count')}。",
        f"- 行情时间: {payload.get('quote_time_min')} 至 {payload.get('quote_time_max')}。",
        f"- 二次检验 A 档: {', '.join(a_names) if a_names else '无'}。",
        "- 规则提示: A 档不等于买入结论，仍需逐家公司核年报、派息政策、关联交易和流动性。",
        "",
        "## 主榜前五",
        "",
    ]
    for idx, row in enumerate(top, start=1):
        lines.append(
            f"{idx}. {row.get('ts_code')} {row.get('name')} "
            f"score={row.get('score'):.1f} cash/liab={row.get('cash_to_liab'):.2f} "
            f"net_cash/mv={row.get('net_cash_to_mv'):.2f}"
        )
    if etfs:
        lines.extend(["", "## 沪深红利ETF前五", ""])
        for idx, row in enumerate(etfs, start=1):
            lines.append(
                f"{idx}. {row.get('ts_code')} {row.get('name')} "
                f"yield={float(row.get('dividend_yield_ttm') or 0):.2%} "
                f"amount={float(row.get('amount_cny') or 0) / 1e8:.2f}亿 score={float(row.get('score') or 0):.1f}"
            )
    if special:
        lines.extend(["", "## 特殊高分红观察池前五", ""])
        for idx, row in enumerate(special, start=1):
            lines.append(
                f"{idx}. {row.get('ts_code')} {row.get('name')} "
                f"label={row.get('special_label')} "
                f"yield={float(row.get('dividend_paid_yield_est') or 0):.2%} "
                f"score={float(row.get('watch_score') or 0):.1f}"
            )
    lines.append("")
    return "\n".join(lines)


def llm_summary(payload: dict, model: str) -> str:
    client = DpskChatClient(model=model)
    system = (
        "你是一个严谨的港股深度价值研究助理。"
        "只基于给定 JSON 总结，不要编造外部事实；输出中文，强调风险和复核点。"
    )
    user = (
        "请把以下港股净现金筛选结果压缩成一页研究摘要："
        "包括当前最值得优先复核的候选、需要降权的标的、分红/回购质量、盈利可持续性、治理风险。"
        "\n\nJSON:\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    return client.complete(system=system, user=user)


def render_html(markdown_text: str) -> str:
    paragraphs = []
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            paragraphs.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            paragraphs.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            paragraphs.append(f"<li>{html.escape(line[2:])}</li>")
        elif line.strip():
            paragraphs.append(f"<p>{html.escape(line)}</p>")
    body = "\n".join(paragraphs)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>流水线汇总</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',Arial,sans-serif;margin:0;background:#f7f8fa;color:#17202a}}main{{max-width:920px;margin:0 auto;padding:18px}}h1{{font-size:24px}}h2{{font-size:18px;margin-top:24px}}li,p{{line-height:1.6}}</style>
</head><body><main>{body}</main></body></html>"""


def build_digest(context: PipelineContext) -> str:
    cfg = context.config
    payload = build_payload(cfg.output_dir)
    if cfg.use_llm:
        summary = llm_summary(payload, cfg.llm_model)
        mode = "dpsk"
    else:
        summary = deterministic_summary(payload)
        mode = "deterministic"
    meta = {"mode": mode, "payload": payload, "summary": summary}
    (cfg.output_dir / "llm_digest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (cfg.output_dir / "llm_digest.md").write_text(summary, encoding="utf-8")
    (cfg.output_dir / "llm_digest.html").write_text(render_html(summary), encoding="utf-8")
    return f"wrote llm_digest.* using {mode}"
