from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelineConfig:
    """Runtime configuration for a local pipeline run.

    Secrets are intentionally referenced by environment variable name only.
    The actual token values never enter this dataclass or any generated file.
    """

    root: Path
    scripts_dir: Path
    output_dir: Path
    public_dir: Path
    python: str
    max_codes: int = 420
    pull_missing: bool = False
    use_llm: bool = False
    llm_model: str = "deepseek-chat"
    dpsk_key_env: str = "DPSK_API_KEY"
    dpsk_base_url_env: str = "DPSK_BASE_URL"

    @classmethod
    def discover(
        cls,
        *,
        root: str | Path | None = None,
        output_dir: str | Path | None = None,
        public_dir: str | Path | None = None,
        max_codes: int = 420,
        pull_missing: bool = False,
        use_llm: bool = False,
        llm_model: str = "deepseek-chat",
    ) -> "PipelineConfig":
        project_root = Path(root or Path.cwd()).resolve()
        resolved_output = Path(output_dir).resolve() if output_dir else project_root
        resolved_public = Path(public_dir).resolve() if public_dir else project_root
        return cls(
            root=project_root,
            scripts_dir=project_root / "scripts",
            output_dir=resolved_output,
            public_dir=resolved_public,
            python=sys.executable,
            max_codes=max_codes,
            pull_missing=pull_missing,
            use_llm=use_llm,
            llm_model=llm_model,
        )

    def stage_env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing_path = env.get("PYTHONPATH", "")
        paths = [str(self.scripts_dir), str(self.root / "src")]
        if existing_path:
            paths.append(existing_path)
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["HK_NETCASH_OUTPUT_DIR"] = str(self.output_dir)
        env["HK_NETCASH_PUBLIC_DIR"] = str(self.public_dir)
        return env
