# 可移植部署设计
采用三种交付：源码+依赖锁（Windows/macOS/Linux）、Docker（Linux）、PyInstaller onedir（构建主机平台）。拒绝 onefile 内嵌全部图库：大文件每次解压慢且不利于只读资产对账。
冻结程序在 parking-system/ 内运行，__file__ 资源根保留 server/core 层级，PyInstaller contents_directory='.'；assets_3d 仍放同级。data 生成在程序可写目录，外部配置 PARKING_CONFIG 覆盖。只打包 demo 图库与兼容 plates、模型、字体、静态资源，完整基准数据随源码交付，不塞进可执行文件。
run.py 不再重建/删除 venv；已存 venv 使用明确解释器，依赖缺失按锁安装；--check 校验并退出，禁止缺 manifest 仍启动为旧布局。生成与 seed 的退出码向上传播，默认不生成/覆盖用户数据。
Docker 构建上下文是交付根目录，拷贝 parking-system 与同级 assets_3d；使用非 root，单 worker SQLite，挂载持久化 data，非演示配置缺省 demo_mode=false。构建不注入密钥，首次生成或环境变量设置凭据。
不选择 pip 安装 hyperlpr3：仓库已带 Apache-2.0 推理子集和本地四个 ONNX，避免上游自动下载和重复 OpenCV。精确锁来自当前验证环境并添加 uvloop 平台标记；跨 Python 小版本可用兼容范围清单，仍需当地 wheel 实验。
