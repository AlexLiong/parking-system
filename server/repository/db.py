"""数据库引擎与会话管理（repository/db.py）。

- SQLite + WAL 模式（并发读、崩溃可恢复，FR-9.1）；
- 使用 ``scoped_session``：每个线程/请求一个会话，服务层显式控制事务边界
  （"先落库后动作"依赖显式 commit，RR-4.1）；
- 建表由 ORM 模型 ``create_all`` 完成（模型即建表，§9）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextvars import ContextVar
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from .models import Base

log = logging.getLogger("db")
request_scope = ContextVar("parking_request_scope", default=None)


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.db.write_lock:
            return method(self, *args, **kwargs)
    return call


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
    """每个新连接开启 WAL、外键约束与正常同步级别。"""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
    except Exception:  # noqa: BLE001 - 非 SQLite 或 pragma 不支持时忽略
        pass


class Database:
    """数据库门面：持有 engine / session 工厂，提供事务作用域。"""

    def __init__(self, db_path: str | Path, *, echo: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{self.db_path.as_posix()}"
        # check_same_thread=False：允许 FastAPI 线程池 / WS 事件循环跨线程使用，
        # 会话隔离由 scoped_session 保证。
        self.engine = create_engine(url, echo=echo, future=True,
                                    connect_args={"check_same_thread": False},
                                    pool_size=10, max_overflow=10, pool_timeout=10)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False,
                                             future=True)
        self.session_registry = scoped_session(self._session_factory,
            scopefunc=lambda: request_scope.get() or ("thread", threading.get_ident()))
        self._local = threading.local()
        self.write_lock = threading.RLock()

    @property
    def session(self) -> Session:
        """当前线程绑定的会话。"""
        return self.session_registry()

    def init_schema(self) -> None:
        columns = {
            "barrier_state": {"keep_open": "BOOLEAN NOT NULL DEFAULT 0"},
            "parking_record": {"session_id": "TEXT", "aborted_reason": "TEXT",
                               "benefit_type": "VARCHAR(16)", "temporary": "BOOLEAN NOT NULL DEFAULT 0",
                               "entry_sim_sec": "FLOAT", "exit_sim_sec": "FLOAT",
                               "duration_sim_sec": "FLOAT"},
            "pending_manual": {"sim_deadline_sec": "FLOAT"},
            "fee_rule": {"created_at": "DATETIME"},
            "payment_record": {"session_id": "TEXT"},
            "recognition_log": {"session_id": "TEXT"},
            "incident_log": {"session_id": "TEXT"},
            "op_log": {"session_id": "TEXT"},
        }
        existing = inspect(self.engine)
        missing = {
            table: [(name, kind) for name, kind in defs.items()
                    if name not in {c["name"] for c in existing.get_columns(table)}]
            for table, defs in columns.items() if existing.has_table(table)
        }
        adding_pending = bool(existing.get_table_names()) and bool(
            set(Base.metadata.tables) - set(existing.get_table_names()))
        adding_indexes = existing.has_table('parking_record') and not {
            'uq_open_plate', 'uq_open_space'
        } <= {i['name'] for i in existing.get_indexes('parking_record')}
        if (any(missing.values()) or adding_pending or adding_indexes) and self.db_path.exists():
            backup = self.db_path.with_name(
                f"{self.db_path.name}.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
            with sqlite3.connect(self.db_path) as source, sqlite3.connect(backup) as target:
                source.backup(target)
            log.info("迁移前数据库备份: %s", backup)
        with self.engine.begin() as conn:
            for table, defs in missing.items():
                for name, kind in defs:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {kind}'))
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_demo_session_active "
                              "ON demo_session(status) WHERE status = 'active'"))
            # P1-09：temporary 是无入场记录的人工出场账单，不是真实在场记录；
            # 开放车牌唯一索引必须排除它，否则同牌真实入场会被旧索引阻断。
            # 旧库可能已有 status='in' 的旧定义，先删除再按新谓词重建。
            conn.execute(text("DROP INDEX IF EXISTS uq_open_plate"))
            for name, table, column, predicate in (
                ("uq_open_plate", "parking_record", "plate", "status = 'in' AND temporary = 0"),
                ("uq_open_space", "parking_record", "space_no", "status = 'in' AND space_no IS NOT NULL"),
                ("uq_occupied_plate", "parking_space", "current_plate", "status = 'occupied' AND current_plate IS NOT NULL"),
            ):
                # Never silently delete or merge conflicting historical data.
                duplicates = conn.execute(text(f'SELECT "{column}", COUNT(*) FROM "{table}" '
                    f'WHERE {predicate} GROUP BY "{column}" HAVING COUNT(*) > 1')).all()
                if duplicates:
                    raise ValueError(f"一致性索引 {name} 存在冲突；请先运行 tools/audit_consistency.py，未删除数据")
                conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS "{name}" ON "{table}" '
                                  f'("{column}") WHERE {predicate}'))
            # P1-13：统计基准（100k 行）的区间扫描/聚合补普通与覆盖索引；为已有库补建，
            # 新库由 Base.metadata.create_all 按模型定义创建。索引只影响查询性能，不改语义。
            # 注意：老库若缺少某列（如极简历史表无 entry_time/exit_time），必须跳过该索引，
            # 否则 SQLite 会把双引号内的未知列名当成“表达式索引”静默创建（无用的垃圾索引）。
            for name, table, columns in (
                ("idx_exit_time", "parking_record", ("exit_time",)),
                ("idx_entry_cover", "parking_record", ("entry_time", "exit_time", "temporary")),
                ("idx_exit_cover", "parking_record", ("exit_time", "entry_time", "temporary")),
                ("idx_paid_at", "payment_record", ("paid_at",)),
                ("idx_paid_at_cover", "payment_record", ("paid_at", "amount_paid")),
            ):
                if not existing.has_table(table):
                    continue
                available = {c["name"] for c in existing.get_columns(table)}
                missing = [c for c in columns if c not in available]
                if missing:
                    log.warning("P1-13 索引 %s 跳过：%s 缺少列 %s", name, table, missing)
                    continue
                cols = ", ".join(f'"{c}"' for c in columns)
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})'))

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        try:
            self.session.rollback()
        except Exception:  # noqa: BLE001
            log.exception("rollback 失败")

    def remove(self) -> None:
        """请求结束移除线程绑定会话（避免连接泄漏）。"""
        self.session_registry.remove()

    @contextmanager
    def scope(self) -> Iterator[Session]:
        with self.write_lock:
            with self._transaction() as session:
                yield session

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        """事务作用域：正常退出提交，异常回滚；不自动 remove（由调用方生命周期决定）。"""
        session = self.session_registry()
        nested = getattr(self._local, "scope_depth", 0) > 0
        self._local.scope_depth = getattr(self._local, "scope_depth", 0) + 1
        callbacks = []
        if not nested:
            self._local.after_commit = []
        try:
            if not nested:
                connection = session.connection()
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                if not session.dirty and not session.new:
                    session.expire_all()
            yield session
            if not nested:
                session.commit()
                callbacks = self._local.after_commit
                self._local.after_commit = []
        except Exception:
            if not nested:
                session.rollback()
                self._local.after_commit = []
            raise
        finally:
            self._local.scope_depth -= 1
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - committed data is still replayable
                log.exception("提交后通知失败；事件可从数据库补发")

    @property
    def in_scope(self) -> bool:
        return getattr(self._local, "scope_depth", 0) > 0

    def on_commit(self, callback) -> None:
        if not self.in_scope:
            raise RuntimeError("on_commit 必须在 db.scope() 内注册")
        self._local.after_commit.append(callback)

    def close(self) -> None:
        self.session_registry.remove()
        self.engine.dispose()


_db: Optional[Database] = None


def init_db(db_path: str | Path) -> Database:
    """初始化全局数据库单例（建表）。"""
    global _db
    _db = Database(db_path)
    _db.init_schema()
    return _db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db()")
    return _db


def set_db(db: Database) -> None:
    global _db
    _db = db
