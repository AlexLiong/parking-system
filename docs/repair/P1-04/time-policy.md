# P1-04 时间口径

数据库中的无时区时间保持 UTC，历史数据无需平移。停车场业务时区由 `server.timezone` 配置，默认 `Asia/Shanghai`。日期统计按该时区的自然日计算，再转换成 UTC 查询范围；跨夏令时日期可为 23 或 25 小时。

REST 输入带偏移的 ISO 时间按偏移解释；无偏移 ISO 时间按停车场业务时区解释。无效日期时间返回校验错误。REST、状态快照和事件历史输出带偏移的 ISO 时间，`/api/overview` 同时返回 `timezone`。管理端和 3D HUD 使用该时区显示，并根据服务端时间校准时钟。

部署时保留原 SQLite 数据。可运行 `python tools/migrate_time_display.py --config <配置文件>` 只读核对数据库路径和显示时区。回滚代码后，历史 UTC 值仍可由旧版本读取；已按新口径写入的无偏移 API 输入会以 UTC 正确存储。

验证：`tests/test_time_policy.py` 覆盖北京时间日界、带偏移与无偏移输入、记录筛选、名单到期、状态和事件时间以及纽约夏令时日界。浏览器复跑脚本与截图位于交付目录 `_qa_audit/repair/P1-04/`。
