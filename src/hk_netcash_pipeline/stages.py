from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .config import PipelineConfig


@dataclass
class StageResult:
    name: str
    ok: bool
    elapsed_s: float
    details: str = ""


@dataclass
class PipelineContext:
    config: PipelineConfig
    results: list[StageResult] = field(default_factory=list)

    def add(self, result: StageResult) -> None:
        self.results.append(result)


class PipelineStage(Protocol):
    name: str

    def run(self, context: PipelineContext) -> StageResult:
        ...


@dataclass(frozen=True)
class ScriptStage:
    name: str
    script: str
    args_factory: Callable[[PipelineConfig], list[str]] = lambda _: []

    def run(self, context: PipelineContext) -> StageResult:
        cfg = context.config
        script_path = cfg.scripts_dir / self.script
        if not script_path.exists():
            raise FileNotFoundError(script_path)
        cmd = [cfg.python, str(script_path), *self.args_factory(cfg)]
        started = time.monotonic()
        proc = subprocess.run(
            cmd,
            cwd=cfg.root,
            env=cfg.stage_env(),
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed = time.monotonic() - started
        output = "\n".join(x for x in [proc.stdout.strip(), proc.stderr.strip()] if x)
        if proc.returncode != 0:
            raise RuntimeError(f"stage {self.name} failed with exit {proc.returncode}\n{output}")
        return StageResult(self.name, True, elapsed, output[-4000:])


@dataclass(frozen=True)
class FunctionStage:
    name: str
    fn: Callable[[PipelineContext], str | None]

    def run(self, context: PipelineContext) -> StageResult:
        started = time.monotonic()
        details = self.fn(context) or ""
        return StageResult(self.name, True, time.monotonic() - started, details)


def ensure_required_artifacts(output_dir: Path, names: list[str]) -> None:
    missing = [name for name in names if not (output_dir / name).exists()]
    if missing:
        raise FileNotFoundError("missing required artifacts: " + ", ".join(missing))
