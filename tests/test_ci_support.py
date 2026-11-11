"""P1-13：CI/基准辅助模块单测（阈值显式配置、node 发现）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ci_support import bench_limit_for, load_bench_config, resolve_node  # noqa: E402


def test_bench_limit_default_is_2s(monkeypatch):
    monkeypatch.delenv("PARKING_BENCH_MAX_SECONDS", raising=False)
    limit, source = bench_limit_for("Windows", config={"default_seconds": 2.0})
    assert limit == 2.0
    assert source == "config:default"


def test_bench_limit_platform_override(monkeypatch):
    monkeypatch.delenv("PARKING_BENCH_MAX_SECONDS", raising=False)
    cfg = {"default_seconds": 2.0, "platforms": {"Windows": 2.5}}
    limit, source = bench_limit_for("Windows", config=cfg)
    assert limit == 2.5 and source == "config:platforms.Windows"
    limit2, source2 = bench_limit_for("Darwin", config=cfg)
    assert limit2 == 2.0 and source2 == "config:default"


def test_bench_limit_env_and_cli_override(monkeypatch):
    monkeypatch.setenv("PARKING_BENCH_MAX_SECONDS", "3.25")
    limit, source = bench_limit_for("Windows", config={"default_seconds": 2.0})
    assert limit == 3.25 and source == "env:PARKING_BENCH_MAX_SECONDS"
    limit2, source2 = bench_limit_for("Windows", override=4.0, config={"default_seconds": 2.0})
    assert limit2 == 4.0 and source2 == "cli:--limit-seconds"


def test_bench_limits_config_file_shape():
    cfg = load_bench_config()
    assert cfg["default_seconds"] == 2.0
    assert "platforms" in cfg and isinstance(cfg["platforms"], dict)


def test_resolve_node_returns_executable():
    node = resolve_node()
    assert node is not None and Path(node).is_file()
