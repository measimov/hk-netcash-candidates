from __future__ import annotations

import shutil
from pathlib import Path

from .stages import PipelineContext


STATIC_ARTIFACTS = [
    "index.html",
    "hk_ranked_candidates.csv",
    "hk_ranked_candidates.md",
    "hk_all_scored_prefilter.csv",
    "hk_prefilter.csv",
    "hk_merged_universe.csv",
    "hk_loose_ranked_candidates.csv",
    "hk_loose_all_scored.csv",
    "hk_loose_prefilter.csv",
    "hk_supplemental_candidates.csv",
    "expanded_quality_candidates.csv",
    "expanded_quality_candidates.md",
    "expanded_quality_candidates.html",
    "governance_risk_overlay.csv",
    "governance_risk_overlay.html",
    "governance_filtered_candidates.csv",
    "secondary_validation_top20.csv",
    "secondary_validation_top20.md",
    "secondary_validation_top20.html",
    "special_dividend_prefilter.csv",
    "special_dividend_all_scored.csv",
    "special_dividend_watchlist.csv",
    "special_dividend_watchlist.md",
    "special_dividend_watchlist.html",
    "special_dividend_annuals.csv",
    "property_service_target_review.csv",
    "property_service_target_review.md",
    "property_service_target_review.html",
    "property_service_target_annuals.csv",
    "a_dividend_etf_rank.csv",
    "a_dividend_etf_rank.md",
    "a_dividend_etf_rank.html",
    "a_dividend_etf_yield_history_stats.csv",
    "a_dividend_etf_universe.csv",
    "a_dividend_etf_realtime_quotes.csv",
    "top20_hkex_announcement_titles.csv",
    "top25_tushare_dividend_trends.csv",
    "top15_dividend_crosscheck_ths.csv",
    "tushare_hk_basic.csv",
    "tushare_hk_latest_daily.csv",
    "tushare_hk_recent_liquidity.csv",
    "tencent_hk_quotes_latest.csv",
    "eastmoney_hk_quotes.csv",
    "hkex_regulatory_2018_2026.csv",
    "hkexnews_active_stock_map.csv",
    "sfc_enforcement_news.csv",
    "llm_digest.json",
    "llm_digest.md",
    "llm_digest.html",
]


def sync_public(context: PipelineContext) -> str:
    cfg = context.config
    if cfg.output_dir.resolve() == cfg.public_dir.resolve():
        return "output_dir and public_dir are the same; no copy needed"
    copied = 0
    cfg.public_dir.mkdir(parents=True, exist_ok=True)
    for name in STATIC_ARTIFACTS:
        src = cfg.output_dir / name
        if not src.exists():
            continue
        dst = cfg.public_dir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return f"copied {copied} artifacts to {cfg.public_dir}"


def write_nojekyll(context: PipelineContext) -> str:
    path = context.config.public_dir / ".nojekyll"
    path.touch()
    return str(path)
