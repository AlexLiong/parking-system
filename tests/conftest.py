"""pytest 全局夹具（tests/conftest.py）。

为每个测试提供**隔离**的配置与容器：数据库/照片/报表落在 pytest 的 tmp_path，
测试之间互不污染。字符模板与合成车牌数据集沿用项目内真实产物（只读）。

约定：
- ``cfg``：深拷贝真实 config.yaml 后把可写路径重定向到 tmp_path，并 set_config 注入；
- ``container``：已 bootstrap（布局/费率/管理员）且 start（订阅事件）的容器；
- ``client``：FastAPI TestClient（跑完整 lifespan，使用同一 tmp 数据库）。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core.config import load_config, set_config  # noqa: E402
from server.gateway.context import Container  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    """隔离配置：可写目录指向 tmp_path，模板/数据集沿用项目真实产物。"""
    base = load_config(force=True)
    c = copy.deepcopy(base)
    c.paths.data_dir = tmp_path / "data"
    c.paths.db_file = tmp_path / "data" / "test.db"
    c.paths.photos_dir = tmp_path / "data" / "photos"
    c.paths.reports_dir = tmp_path / "data" / "reports"
    c.paths.logs_dir = tmp_path / "data" / "logs"
    c.ensure_dirs()
    set_config(c)
    yield c
    set_config(base)


@pytest.fixture
def container(cfg):
    """已装配并启动事件订阅的容器（每个测试独立 tmp 数据库）。"""
    c = Container(cfg)
    c.bootstrap()
    c.start()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def client(cfg):
    """FastAPI TestClient：跑完整 lifespan，容器使用同一 tmp 数据库。"""
    from fastapi.testclient import TestClient

    from server.main import create_app

    app = create_app(cfg)
    with TestClient(app) as cli:
        yield cli


@pytest.fixture
def admin_token(client):
    """登录默认管理员，返回鉴权头。"""
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}
