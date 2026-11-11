"""配置加载（core/config.py）。

所有模块只从 ``get_config()`` 读配置；配置与代码分离（可维护性 NR）。
路径一律用 ``pathlib`` 解析为绝对路径（跨平台约束，禁止硬编码分隔符）。
支持通过环境变量 ``PARKING_CONFIG`` 指定配置文件，便于测试用独立配置。
"""
from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# 项目根目录：server/core/config.py -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _resolve(p: str | Path) -> Path:
    """把配置里的相对路径解析为相对项目根的绝对路径。"""
    path = Path(p)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = field(default_factory=lambda: ["http://127.0.0.1:8000", "http://localhost:8000"])
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    jwt_expire_minutes: int = 720
    ws_v2: bool = True
    timezone: str = "Asia/Shanghai"
    demo_mode: bool = False


@dataclass
class PathsConfig:
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    db_file: Path = field(default_factory=lambda: PROJECT_ROOT / "data/parking.db")
    photos_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data/photos")
    reports_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data/reports")
    plates_dataset: Path = field(default_factory=lambda: PROJECT_ROOT / "datasets/demo-lpr")
    web_admin: Path = field(default_factory=lambda: PROJECT_ROOT / "web/admin")
    web_sim: Path = field(default_factory=lambda: PROJECT_ROOT / "web/sim3d")
    logs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data/logs")


@dataclass
class LPRConfig:
    conf_threshold: float = 0.85
    char_conf_threshold: float = 0.45
    max_image_side: int = 1280
    locate_topk: int = 3
    disambig_gap: float = 0.15
    backend: str = "auto"
    onnx_model: Path = field(default_factory=lambda: PROJECT_ROOT / "server/lpr/models/char_cnn.onnx")
    template_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "server/lpr/models/templates")
    blue_size: tuple[int, int] = (440, 140)
    green_size: tuple[int, int] = (480, 140)
    char_size: tuple[int, int] = (20, 40)
    retry_before_manual: int = 3
    manual_confirm_timeout: int = 60
    multiframe_enabled: bool = True
    style_refinement: bool = True
    # Idea A：真实照片字符分类器复核。产物缺失时 RealCharVerifier 自动跳过；
    # 置 false 可完全关闭，不改变 HyperLPR3 原有 0.95 安全阈值。
    real_char_verify: bool = False
    real_char_model: Path = field(
        default_factory=lambda: PROJECT_ROOT / "server/lpr/models/char_cnn_real.onnx")
    real_char_templates: Path = field(
        default_factory=lambda: PROJECT_ROOT / "server/lpr/models/real_char_templates")
    real_char_knn: Path = field(
        default_factory=lambda: PROJECT_ROOT / "server/lpr/models/real_char_knn.npz")
    # 统一识别管线：原图识别；低置信/无结果时依次尝试 bbox 裁剪、旋转校正、
    # 2x 放大、CLAHE 等简单变体，选置信度最高结果（HyperLPR3 仍为主）。
    pipeline_variants: bool = True
    pipeline_max_variants: int = 6
    # 仿真 3D 相机截图 + expected_plate：识别串规范化后相等且 conf 达到本阈值
    # 可覆盖 need_manual 放行（0.80~0.85；上传识别仍用 conf_threshold=0.85）。
    sim_conf_threshold: float = 0.85


@dataclass
class AccessConfig:
    barrier_open_sec: float = 1.5
    barrier_close_sec: float = 1.5
    keep_open_sec: float = 3.0
    close_delay_sec: float = 1.0
    min_follow_gap_m: float = 1.0
    barrier_ack_timeout_sec: float = 3.0
    barrier_retry: int = 1
    tick_interval_ms: int = 50
    exit_no_record_policy: str = "max_duration"
    temp_max_hours: int = 24


@dataclass
class SpacesConfig:
    total_default: int = 80
    c_reserve: int = 0
    space_width_m: float = 3.0
    space_depth_m: float = 6.0


@dataclass
class BillingConfig:
    free_minutes: int = 15
    first_hour_price: float = 5.0
    unit_minutes: int = 30
    unit_price: float = 2.0
    daily_cap: float = 40.0
    ne_discount: float = 0.9
    round_up: bool = True


@dataclass
class ReportConfig:
    font_name: str = "STSong-Light"
    matplotlib_dpi: int = 130


@dataclass
class RecoveryConfig:
    ws_heartbeat_sec: int = 5
    camera_fail_streak: int = 3
    barrier_safe_state: str = "keep_open"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data/logs")
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 7


@dataclass
class RetentionConfig:
    """上传/相机/视频抽帧文件保留期（保守默认：7 天 / 2 GiB）。"""
    enabled: bool = True
    max_age_days: float = 7.0          # 早于该天数的文件按容量策略清理
    max_total_mb: int = 2048           # photos 三个子目录总容量上限（MiB）
    keep_recent_files: int = 50        # 每个子目录至少保留最新 N 个文件（证据/最近识别）
    min_age_hours_for_capacity: int = 24  # 容量清理不删除该小时内文件
    interval_hours: float = 6.0        # 服务后台清理周期（小时）
    run_on_startup: bool = True        # 服务启动时先清理一次


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    lpr: LPRConfig = field(default_factory=LPRConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    spaces: SpacesConfig = field(default_factory=SpacesConfig)
    billing: BillingConfig = field(default_factory=BillingConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    default_layout: list[dict] = field(default_factory=list)
    scene: dict[str, Any] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def ensure_dirs(self) -> None:
        for d in (self.paths.data_dir, self.paths.photos_dir,
                  self.paths.reports_dir, self.paths.logs_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


def _build(raw: dict) -> AppConfig:
    paths_raw = dict(raw.get("paths", {}))
    log_raw = dict(raw.get("logging", {}))
    paths = PathsConfig(**{k: _resolve(v) for k, v in paths_raw.items()})
    lpr_raw = dict(raw.get("lpr", {}))
    for k in ("onnx_model", "template_dir", "real_char_model",
              "real_char_templates", "real_char_knn"):
        if k in lpr_raw:
            lpr_raw[k] = _resolve(lpr_raw[k])
    if "blue_size" in lpr_raw:
        lpr_raw["blue_size"] = tuple(lpr_raw["blue_size"])
    if "green_size" in lpr_raw:
        lpr_raw["green_size"] = tuple(lpr_raw["green_size"])
    if "char_size" in lpr_raw:
        lpr_raw["char_size"] = tuple(lpr_raw["char_size"])
    logging_cfg = LoggingConfig(
        level=log_raw.get("level", "INFO"),
        dir=_resolve(log_raw.get("dir", "data/logs")),
        max_bytes=log_raw.get("max_bytes", 5 * 1024 * 1024),
        backup_count=log_raw.get("backup_count", 7),
    )
    return AppConfig(
        server=ServerConfig(**raw.get("server", {})),
        paths=paths,
        lpr=LPRConfig(**lpr_raw),
        access=AccessConfig(**raw.get("access", {})),
        spaces=SpacesConfig(**raw.get("spaces", {})),
        billing=BillingConfig(**raw.get("billing", {})),
        report=ReportConfig(**raw.get("report", {})),
        recovery=RecoveryConfig(**raw.get("recovery", {})),
        logging=logging_cfg,
        retention=RetentionConfig(**raw.get("retention", {})),
        default_layout=raw.get("default_layout", []),
        scene=raw.get("scene", {}),
        raw=raw,
    )


_lock = threading.Lock()
_config: Optional[AppConfig] = None
_config_path: Optional[Path] = None


def load_config(path: Optional[str | Path] = None, *, force: bool = False) -> AppConfig:
    """加载并缓存配置。测试可传 ``path`` + ``force=True`` 重载独立配置。"""
    global _config, _config_path
    cfg_path = Path(path or os.environ.get("PARKING_CONFIG") or DEFAULT_CONFIG_PATH)
    with _lock:
        if _config is not None and not force and cfg_path == _config_path:
            return _config
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}
        _config = _build(raw)
        _config_path = cfg_path
        return _config


def get_config() -> AppConfig:
    """获取全局配置单例（首次调用自动加载默认配置文件）。"""
    global _config
    if _config is None:
        return load_config()
    return _config


def set_config(cfg: AppConfig) -> None:
    """显式注入配置（测试 / 自定义启动用）。"""
    global _config
    with _lock:
        _config = cfg
