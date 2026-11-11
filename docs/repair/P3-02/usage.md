# 构建与启动
源码需要 Python 3.11–3.14；精确锁实测 Python 3.14/macOS arm64，Linux arm64 验证结果见修复报告。首次联网安装，日常推理完全本地。HyperLPR3 源码与 Apache-2.0、ONNX 权重已随包提供，不要另外安装上游 hyperlpr3/opencv-python。

Windows PowerShell：`python tools/run.py --check`，然后 `./start.ps1`。
macOS：`python3 tools/run.py --check`，然后 `./start.command`。
默认监听 127.0.0.1:8000；管理端 `/admin/`，仿真 `/sim/`。`--venv` 选择独立环境；其他 Python 小版本安装锁失败时，使用 `--compatible-deps` 解析兼容范围并在当地重新测试。

课程 config.yaml 显式 demo_mode=true，有 admin/admin123 演示凭据，仅供本地课程演示。非演示使用环境变量 PARKING_CONFIG 指向 config.production.yaml；新库首启写入 data/.admin-bootstrap（随机密码）、data/.jwt-secret，或用 PARKING_ADMIN_PASSWORD/PARKING_JWT_SECRET 显式设置。现有演示库切换 false 不会修改已有账户密码，须更换账户/密码或用全新隔离 data 目录。非演示并非生产认证方案。

PyInstaller：`python -m pip install -r requirements-build.txt`，`python tools/build_native.py --output ../native-dist`（输出须不存在）。每个目标系统/架构分别构建。交付中 native-dist 内 parking-system 与 assets_3d 保持同级。运行 parking（Windows parking.exe）；`--check` 校验本地资源，`--port` 指定端口。首次操作系统校验/加载较慢，实测结果见报告。

Docker：在交付根目录 `docker build -f parking-system/Dockerfile -t parking-course .`，`docker run --name parking-course -p 127.0.0.1:8000:8000 -v parking-data:/app/parking-system/data parking-course`。配置缺省非演示；读取容器 data/.admin-bootstrap 获取首启密码。单 worker，SQLite/控制器不支持多进程一致性。可用 PARKING_BASE_IMAGE 构建参数指定可信公共镜像缓存及 digest。本次实际基础镜像 digest 记录于 evidence。

资产不能删除/重导出；缺少同级 assets_3d 时启动工具拒绝继续。备份 data 全目录后迁移；停进出场并对账后回滚代码及对应 DB 备份，不用旧程序直接写入新库。
