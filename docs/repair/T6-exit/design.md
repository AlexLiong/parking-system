# P1-09 / P2-14 / P2-11 出场策略、人工记账与原因
无记录出口原guard无条件异常；manual_exit仅写abnormal不写付款。配置策略采用max_duration/manual/free（保留void作manual别名）。max_duration创建临时停车记录（temporary=true、无车位、进场时间推定now-temp_max_hours），只报价不抬杆；manual入原pending，管理员选择按最长时长、免费、作废或提供实际进场时间。free生成零元付款后抬杆。临时记录不占车位，不计入虚构进场/峰值/平均时长，实际离场计车次和付款营收。临时标识可审计，不伪装真实进场。
人工出口有记录默认charge，经正常billing.pay原子付款/出场/释放/提交后抬杆；free写0元付款且保留应收与减免原因；void标abnormal/aborted_reason、0元void账单，作为异常放行留审计；作废不是删除记录。无记录人工选择同样路径，manual_time需合法过去ISO时间。调用可传record_id确保重复请求幂等；未提供时查最后已完成的同牌记录防止网络重试重复造临时订单，但新一次无记录到达必须有vehicle_id区分。负数/NaN金额拒绝。
新增parking_record.temporary additive默认false、自动备份。兼容旧调用默认charge；旧测试abnormal期望更新为付款出场。正常识别入口把实际conf透传，拒绝事件携带原因；不将识别成功conf伪造为0。
验收覆盖三种策略、四种人工处理、收入对账/释放/幂等、临时记录不影响occupancy、非法金额和时间、pending日志审计。无资产修改；提交独立可回退，回滚保留列与历史账单。
