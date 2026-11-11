# T4 验收

401/403保护全路由，legacy/v2WS无token/伪造/过期/Origin失败，关闭demo模式不种已知管理员/不签demo token，CORS反例，路径越界与上传上限，登出旧token失败、登录限流。

20/50并发overview/不同牌进场/同牌重放/pay/exit，检查0个500、同牌仅1在场、车位单占、payment1次、事务回滚、线程连接释放；复查SQL对账、动作ID与序号。全pytest/Node与浏览器登录/3D/报表正常，副本迁移备份和索引冲突不破坏源数据。
