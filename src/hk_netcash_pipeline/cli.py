from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

from .config import PipelineConfig
from .digest import build_digest
from .publish import sync_public, write_nojekyll
from .stages import FunctionStage, PipelineContext, PipelineStage, ScriptStage


def _primary_args(cfg: PipelineConfig) -> list[str]:
    return ["--resume", "--max-codes", str(cfg.max_codes)]


def _market_args(cfg: PipelineConfig) -> list[str]:
    args = ["--max-codes", str(cfg.max_codes)]
    if cfg.pull_missing:
        args.append("--pull-missing")
    return args


STAGE_REGISTRY: dict[str, PipelineStage] = {
    "primary": ScriptStage("primary", "hk_netcash_screen.py", _primary_args),
    "market-refresh": ScriptStage("market-refresh", "hk_refresh_market_ranking.py", _market_args),
    "supplemental": ScriptStage("supplemental", "hk_supplemental_extract.py"),
    "quality": ScriptStage("quality", "hk_extended_quality_pool.py"),
    "governance": ScriptStage("governance", "hk_governance_risk_overlay.py"),
    "secondary": ScriptStage("secondary", "hk_secondary_validation.py"),
    "property": ScriptStage("property", "hk_target_property_review.py"),
    "render": ScriptStage("render", "hk_render_public_index.py"),
    "digest": FunctionStage("digest", build_digest),
    "sync-public": FunctionStage("sync-public", sync_public),
    "nojekyll": FunctionStage("nojekyll", write_nojekyll),
}


PROFILES: dict[str, list[str]] = {
    "full": [
        "primary",
        "market-refresh",
        "supplemental",
        "quality",
        "governance",
        "secondary",
        "property",
        "render",
        "digest",
        "sync-public",
        "nojekyll",
    ],
    "refresh": [
        "market-refresh",
        "supplemental",
        "quality",
        "governance",
        "secondary",
        "property",
        "render",
        "digest",
        "sync-public",
        "nojekyll",
    ],
    "publish-only": ["render", "digest", "sync-public", "nojekyll"],
    "llm-only": ["digest"],
}


def parse_stage_list(value: str | None, profile: str) -> list[str]:
    if not value:
        return PROFILES[profile]
    stages = [x.strip() for x in value.split(",") if x.strip()]
    unknown = [x for x in stages if x not in STAGE_REGISTRY]
    if unknown:
        raise SystemExit(f"unknown stages: {', '.join(unknown)}")
    return stages


def run_pipeline(config: PipelineConfig, stage_names: Iterable[str]) -> PipelineContext:
    context = PipelineContext(config)
    for name in stage_names:
        stage = STAGE_REGISTRY[name]
        print(f"==> {name}")
        result = stage.run(context)
        context.add(result)
        if result.details:
            print(result.details)
        print(f"<== {name} ok in {result.elapsed_s:.1f}s")
    return context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the HK net-cash candidate pipeline.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="refresh")
    parser.add_argument("--stages", help="comma-separated stage override")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--output-dir", help="artifact directory; defaults to root")
    parser.add_argument("--public-dir", help="static site directory; defaults to root")
    parser.add_argument("--max-codes", type=int, default=420)
    parser.add_argument("--pull-missing", action="store_true", help="allow Tushare pulls for missing financial cache")
    parser.add_argument("--use-llm", action="store_true", help="call DPSK/DeepSeek for digest generation")
    parser.add_argument("--llm-model", default="deepseek-chat")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = PipelineConfig.discover(
        root=args.root,
        output_dir=args.output_dir,
        public_dir=args.public_dir,
        max_codes=args.max_codes,
        pull_missing=args.pull_missing,
        use_llm=args.use_llm,
        llm_model=args.llm_model,
    )
    stage_names = parse_stage_list(args.stages, args.profile)
    run_pipeline(cfg, stage_names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
