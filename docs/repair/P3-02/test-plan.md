1. 保存旧锁与启动脚本问题。
2. fresh venv pip install lock，pip check/full pytest、本地 ONNX hash、demo绿牌推理。
3. 缺 manifest/模型/依赖校验须退出非零；不更改原资产。
4. PyInstaller onedir 同平台构建，分离临时data，实际HTTP登录/页面/模型推理。
5. Docker构建与health（引擎/网络可用时），否则记录限制。
6. 补丁反向dry-run；revert回滚，无DDL。
