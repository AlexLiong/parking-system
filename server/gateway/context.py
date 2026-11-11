"""应用装配容器（gateway/context.py）。

把分散的模块组装成一个可注入的对象图（依赖注入根）。所有服务在此**一次性构建**
并共享同一 ``Database`` / ``EventBus``，遵循低耦合约束：模块之间只通过事件与
抽象接口交互，容器只负责"接线"。

FastAPI 的 lifespan 负责创建容器、执行启动恢复、启动后台任务；路由通过依赖
``get_container`` 取用。
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from collections import defaultdict, deque
from typing import Optional

from ..access import (AccessControlService, ConsistencyGuard, SpaceAllocator)
from ..billing import BillingService
from ..core.config import AppConfig, get_config, load_config, set_config
from ..core.events import EventBus
from ..layout import LayoutService
from ..lpr import LPRService, PlateRecognizer
from ..lpr.hyperlpr_adapter import DemoAwareRecognizer, HyperPlateRecognizer
from ..recovery import CameraWatchdog, IncidentService, StartupRecovery
from ..report import PdfReportGenerator, ReportService, StatisticsService
from ..repository.db import Database, init_db
from ..repository.fee_repo import FeeRuleRepo
from ..repository.log_repo import (IncidentRepo, OpLogRepo, RecognitionLogRepo)
from ..repository.parking_repo import ParkingRecordRepo, PaymentRepo
from ..repository.pending_repo import PendingManualRepo
from ..repository.space_repo import SpaceRepo
from ..repository.user_repo import UserRepo, VehicleListRepo
from ..state import StateService

log = logging.getLogger("gateway.context")


def _build_lpr(cfg: AppConfig, parking_repo, list_repo) -> Optional[LPRService]:
    """构建识别服务（模板/ONNX 缺失时降级为 None，access 转人工，绝不崩溃）。"""
    try:
        if cfg.lpr.backend == "hyperlpr":
            recognizer = HyperPlateRecognizer(cfg.lpr)
        elif cfg.lpr.backend == "auto":
            recognizer = DemoAwareRecognizer(cfg.lpr)
        else:
            recognizer = PlateRecognizer(cfg.lpr)
        # 识别日志由 access 统一写（含 gate 上下文），此处 log_repo=None 避免重复
        return LPRService(recognizer, cfg.lpr, record_repo=parking_repo,
                          log_repo=None, list_repo=list_repo)
    except Exception:  # noqa: BLE001 - 识别不可用不应阻断系统启动
        log.exception("构建 LPR 识别服务失败，进出场将降级为人工确认")
        return None


class Container:
    """对象图根：持有 db / bus / repos / services。"""

    def __init__(self, cfg: Optional[AppConfig] = None, *, db: Optional[Database] = None):
        self.cfg = cfg or get_config()
        set_config(self.cfg)
        self.cfg.ensure_dirs()
        supplied_secret = os.environ.get("PARKING_JWT_SECRET")
        if supplied_secret:
            if len(supplied_secret) < 32:
                raise ValueError("PARKING_JWT_SECRET 至少32字符")
            self.cfg.server.jwt_secret = supplied_secret
        elif self.cfg.server.jwt_secret.startswith("change-me"):
            secret_file = self.cfg.paths.data_dir / '.jwt-secret'
            try:
                fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self.cfg.server.jwt_secret = secret_file.read_text().strip()
            else:
                with os.fdopen(fd,'w') as output:
                    output.write(secrets.token_urlsafe(48))
                self.cfg.server.jwt_secret = secret_file.read_text().strip()
            if len(self.cfg.server.jwt_secret) < 32:
                raise ValueError("本地JWT密钥文件无效")
        self.db = db or init_db(self.cfg.paths.db_file)
        self.bus = EventBus()

        # ---- 仓储 ----
        self.space_repo = SpaceRepo(self.db)
        self.parking_repo = ParkingRecordRepo(self.db)
        self.payment_repo = PaymentRepo(self.db)
        self.fee_repo = FeeRuleRepo(self.db)
        self.incident_repo = IncidentRepo(self.db)
        self.recog_repo = RecognitionLogRepo(self.db)
        self.pending_repo = PendingManualRepo(self.db)
        self.oplog_repo = OpLogRepo(self.db)
        self.user_repo = UserRepo(self.db)
        self.list_repo = VehicleListRepo(self.db)
        self.state = StateService(self)

        # ---- 识别（可降级）----
        self.lpr = _build_lpr(self.cfg, self.parking_repo, self.list_repo)

        # ---- 异常/恢复 ----
        self.incidents = IncidentService(self.incident_repo, self.bus, self.db)
        self.watchdog = CameraWatchdog(
            self.incidents, fail_streak=self.cfg.recovery.camera_fail_streak)

        # ---- 布局 ----
        self.layout = LayoutService(self.space_repo, self.bus, self.cfg)

        # ---- 计费（同时充当 access 的费率快照提供者）----
        self.billing = BillingService(
            db=self.db, fee_repo=self.fee_repo, parking_repo=self.parking_repo,
            payment_repo=self.payment_repo, bus=self.bus, cfg=self.cfg)

        # ---- 进出场 ----
        self.allocator = SpaceAllocator(
            self.space_repo, self.bus, c_reserve=self.cfg.spaces.c_reserve)
        self.guard = ConsistencyGuard(self.parking_repo, self.space_repo)
        self.access = AccessControlService(
            bus=self.bus, db=self.db, parking_repo=self.parking_repo,
            space_repo=self.space_repo, incident_repo=self.incident_repo,
            recog_log_repo=self.recog_repo, allocator=self.allocator,
            guard=self.guard, cfg=self.cfg, recognizer=self.lpr,
            fee_provider=self.billing, billing=self.billing,
            oplog_repo=self.oplog_repo, pending_repo=self.pending_repo,
            list_repo=self.list_repo)

        # ---- 报表 ----
        self.stats = StatisticsService(self.parking_repo, self.payment_repo)
        self.reports = ReportService(self.parking_repo, self.payment_repo)
        self.pdf = PdfReportGenerator(self.cfg)

        # ---- 启动恢复 ----
        self.recovery = StartupRecovery(
            db=self.db, parking_repo=self.parking_repo,
            payment_repo=self.payment_repo, space_repo=self.space_repo,
            incident_service=self.incidents, guard=self.guard,
            access_service=self.access)

        # 广播器由 main 注入事件循环后挂载
        from ..core.simulation import SimulationClock
        self.simulation = SimulationClock(self)
        self.access.simulation = self.simulation
        self.billing.simulation = self.simulation
        self.broadcaster = None
        self.login_attempts = defaultdict(deque)
        self.login_lock = threading.Lock()

    # ----------------------------- 生命周期 -----------------------------
    def bootstrap(self) -> dict:
        """初始化缺省数据：默认布局 + 默认费率 + 默认管理员。返回摘要。"""
        summary: dict = {}
        with self.db.scope():
            summary["layout_seeded"] = self.layout.bootstrap_default()
        with self.db.scope():
            self.billing.ensure_default_rule()
            summary["fee_rules"] = len(self.fee_repo.list_rules())
        with self.db.scope():
            created = False
            if self.user_repo.get_by_username("admin") is None:
                password = os.environ.get("PARKING_ADMIN_PASSWORD")
                if not password and self.cfg.server.demo_mode:
                    password = "admin123"
                elif not password:
                    password = secrets.token_urlsafe(18)
                    credentials = self.cfg.paths.data_dir / 'bootstrap-credentials.txt'
                    fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd,'w') as output:
                        output.write("username=admin\npassword="+password+"\n")
                self.user_repo.create(username="admin", password=password,
                                      role="admin")
                created = True
            summary["admin_created"] = created
            # 课程演示默认提供操作员账号；非 demo 模式仅当显式设置环境变量时创建。
            operator_created = False
            if self.user_repo.get_by_username("operator") is None:
                operator_password = os.environ.get("PARKING_OPERATOR_PASSWORD")
                if not operator_password and self.cfg.server.demo_mode:
                    operator_password = "operator123"
                if operator_password:
                    self.user_repo.create(username="operator", password=operator_password,
                                          role="operator")
                    operator_created = True
            summary["operator_created"] = operator_created
        self.state.ensure_session()
        log.info("容器 bootstrap 完成: %s", summary)
        return summary

    def start(self) -> None:
        """启动事件驱动服务（订阅总线）。"""
        self.access.start()

    def close(self) -> None:
        try:
            self.bus.unsubscribe_all()
        finally:
            self.db.close()


_container: Optional[Container] = None


def build_container(cfg: Optional[AppConfig] = None) -> Container:
    global _container
    _container = Container(cfg)
    return _container


def get_container() -> Container:
    if _container is None:
        raise RuntimeError("容器尚未初始化")
    return _container


def set_container(c: Optional[Container]) -> None:
    global _container
    _container = c
