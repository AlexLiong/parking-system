// 管理端 i18n：导航 key + 全站中文短语字典 + DOM 本地化。
//
// 设计：
// - `t(key)` 保留 key 机制（导航/登录等少量结构 key）；
// - 主要页面文案统一走 `PHRASES` / `translateText()`，由 app.js 在视图挂载后
//   `translateDom()` 并监听后续动态 DOM（WS/轮询/弹窗/toast）自动翻译；
// - `zh-CN` 原样返回，en 下翻译页面标题、按钮、表头、状态枚举、常见提示/错误。
const words={
 'zh-CN':{monitor:'实时监控',recognize:'车牌识别',scenario:'演示剧本',records:'停车记录',find:'反向寻车',stats:'统计报表',fee:'费率管理',layout:'车位布局',lists:'名单管理',incidents:'异常处理',close:'关闭',ok:'确定',cancel:'取消',skip:'跳到主要内容',nav:'页面导航',language:'导航语言',logout:'退出',sim:'打开 3D 仿真端',username:'用户名',password:'密码',login:'登 录',logging:'登录中…',retry:'重试',loading:'加载中…',loadFailed:'加载失败',groupMonitor:'监控',groupOps:'运营',groupConfig:'配置',groupMaintenance:'运维',brand:'停车场管理端',health:'服务健康检查',admin:'管理员'},
 en:{monitor:'Live monitor',recognize:'Plate recognition',scenario:'Demo scenario',records:'Parking records',find:'Find my car',stats:'Statistics',fee:'Fee rules',layout:'Parking layout',lists:'Access lists',incidents:'Incidents',close:'Close',ok:'OK',cancel:'Cancel',skip:'Skip to main content',nav:'Page navigation',language:'Navigation language',logout:'Sign out',sim:'Open 3D simulation',username:'Username',password:'Password',login:'Sign in',logging:'Signing in…',retry:'Retry',loading:'Loading…',loadFailed:'Load failed',groupMonitor:'Monitoring',groupOps:'Operations',groupConfig:'Configuration',groupMaintenance:'Maintenance',brand:'Parking Admin',health:'Service health',admin:'Administrator'}
};

export const locale=(typeof localStorage!=='undefined'&&localStorage.getItem('parking.admin.locale')==='en')?'en':'zh-CN';
export const t=key=>words[locale][key]||words['zh-CN'][key]||key;

// 常见 UI 中文 → English。优先精确匹配；动态数量/状态用下方 patterns 兼容。
const PHRASES={
 // 外壳 / 导航 / 登录
 '停车场管理端':'Parking Admin','停车场收费系统':'Parking Management System','管理控制台 · Admin Console':'Admin Console',
 '监控':'Monitoring','运营':'Operations','配置':'Configuration','运维':'Maintenance','中文':'Chinese','管理员':'Administrator',
 '退出':'Sign out','跳到主要内容':'Skip to main content','页面导航':'Page navigation','导航语言':'Navigation language',
 '打开 3D 仿真端':'Open 3D simulation','服务健康检查':'Service health','无构建原生 ES':'No-build native ES',
 '登录':'Sign in','登 录':'Sign in','登录中…':'Signing in…','用户名':'Username','密码':'Password',
 '请输入用户名和密码':'Enter username and password','登录失败':'Login failed','请使用管理员或操作员账号登录':'Sign in with an admin or operator account',
 '登录已失效，请重新登录':'Session expired. Please sign in again.','已退出登录':'Signed out',
 // 通用
 '加载中…':'Loading…','加载失败':'Load failed','刷新':'Refresh','清空':'Clear','重试':'Retry','关闭':'Close','取消':'Cancel','确定':'OK',
 '保存':'Save','详情':'Details','操作':'Actions','删除':'Delete','编辑':'Edit','查询':'Search','重置':'Reset',
 '上一页':'Previous','下一页':'Next','导出':'Export','导入':'Import','应用变更':'Apply changes','提交修订':'Submit revision',
 '生成':'Generate','导出 PDF':'Export PDF','暂无数据':'No data',
 '暂无操作日志':'No operation logs','暂无分区':'No zones','暂无分区数据':'No zone data','暂无车位数据':'No space data',
 '无匹配记录':'No matching records','名单为空':'List is empty','无异常事件 🎉':'No incidents 🎉','尚未缴费':'Unpaid',
 '该记录已缴费':'This record is already paid','该记录未留存入场照片':'No entry photo is stored for this record',
 '入场照片暂不可用':'Entry photo unavailable','图片不可用':'Image unavailable','无可用图片':'No image available',
 // 状态枚举
 '在场':'On site','已离场':'Exited','待缴费':'Payment due','异常':'Exception','异常放行':'Exception release','会话作废':'Session void',
 '关闭':'Closed','开启':'Open','开启中':'Opening','保持开启':'Held open','关闭中':'Closing','故障':'Fault','已停杆':'Stopped',
 '空闲':'Free','占用':'Occupied','普通':'Standard','新能源优先':'EV preferred','普通车辆':'Standard vehicle',
 '白名单':'Allowlist','黑名单':'Blocklist','月租':'Monthly','未闭环':'Open','已处理':'Resolved','在线':'Online','已过期':'Expired',
 '生效中':'Active','长期':'Long-term','人工':'Manual','自动':'Automatic','自动识别':'Auto recognition','人工录入':'Manual entry',
 '成功':'Success','未知':'Unknown','是':'Yes','否':'No','进一':'Round up','舍去':'Round down','进一法':'Round up',
 // 监控
 '实时监控':'Live monitor','总览 · 设备 · 实时事件 · 人工干预':'Overview · devices · live events · manual actions',
 '总览 · 设备 · 实时事件':'Overview · devices · live events',
 '图像源不可用，请人工确认':'Image source unavailable; manual confirmation required',
 '空闲车位':'Free spaces','在场车辆':'Vehicles on site','未闭环异常':'Open incidents','布局版本':'Layout version',
 '当前在场':'Currently on site','需处理':'Needs attention','运行正常':'Running normally','闸口 / 摄像头':'Gates / cameras',
 '道闸状态':'Barrier status','入口':'Entry','出口':'Exit','摄像头':'Camera','分区余位':'Free by zone',
 '实时事件流':'Live event stream','连接中':'Connecting','实时在线':'Live','连接断开':'Disconnected',
 '推荐车位：—':'Recommended: —','当前无待确认车辆':'No vehicles pending confirmation','待确认车辆原图':'Pending vehicle photo',
 '输入 7/8 位车牌':'Enter a 7/8-character plate','确认':'Confirm','拒绝':'Reject','车辆':'Vehicle','待人工':'Manual review',
 '置信':'Confidence','待人工确认':'Pending manual confirmation','人工干预':'Manual actions','落杆':'Close barrier',
 '抬杆':'Raise barrier','摄像头故障':'Camera fault','恢复':'Restore',
 '清场':'Reset lot','摄像头故障':'Camera fault','恢复':'Restore','选择车牌类型':'Select plate type',
 '蓝牌（燃油）':'Blue plate (fuel)','绿牌（新能源）':'Green plate (EV)','复位演示会话':'Reset demo session',
 '识别输入':'Recognition input',
 '尚未收到识别事件':'No recognition event yet',
 '已关闭会话':'Closed session','释放':'released','个车位':'spaces','状态同步失败':'Status sync failed',
 '事件类型':'Event type','车牌识别':'Plate recognition','出口报价':'Exit quote','道闸指令':'Barrier command','道闸状态':'Barrier status',
 '余位更新':'Free-space update','车位分配':'Space assignment','布局变更':'Layout changed','异常处理':'Incident handling',
 '演示复位':'Demo reset','人工确认结果':'Manual confirmation result','待人工':'Manual review',
 // 记录
 '停车记录':'Parking records','进出场明细 · 缴费信息 · 车牌照片':'Entry/exit details · payments · plate photos',
 '车牌（模糊）':'Plate (fuzzy)','入场起':'Entry from','入场止':'Entry to','状态':'Status','全部状态':'All statuses',
 '入场(本地)':'Entry (local)','出场(本地)':'Exit (local)','费率版本':'Fee version','来源':'Source','车牌':'Plate','车位':'Space','ID':'ID',
 '报价':'Quote','记录详情':'Record details','缴费信息':'Payment info','应付':'Amount due','实付':'Amount paid','时长':'Duration',
 '支付方式':'Payment method','支付时间':'Paid at','计费明细':'Billing details','入场照片':'Entry photo','入场车牌裁剪':'Entry plate crop',
 '出场照片':'Exit photo','出场车牌裁剪':'Exit plate crop','入场时间':'Entry time','出场时间':'Exit time','入场方式':'Entry method',
 '优惠资格':'Benefit','无记录临时结算（时间补录或推定）':'Temporary settlement (time backfilled/estimated)',
 '白名单（本次免费）':'Allowlist (free this time)','月租（本次免费）':'Monthly (free this time)',
 '停放时长':'Parking duration','免费时长':'Free minutes','计费时长':'Billable duration','首小时':'First hour',
 '续时单位':'Additional unit (min)','续时单价':'Additional unit price','续时单位数':'Additional units',
 '单日封顶':'Daily cap','应收':'Amount due','未封顶':'Not capped','已封顶':'Capped',
 '新能源折扣':'EV discount','摘要':'Summary','其他字段':'Other fields',
 '自动扣费':'Auto charge','扫码':'Scan','月租':'Monthly','历史费率（与当前不同）':'Historical fee rule (differs from current)',
 '人工结算':'Manual settlement','结算缴费':'Settle payment','免费放行':'Free release','缴费失败':'Payment failed','报价失败':'Quote failed',
 // 统计
 '统计报表':'Statistics','进出场 / 营收 / 时长 / 峰值占用':'Entries/exits / revenue / duration / peak occupancy',
 '统计周期':'Period','基准日期（anchor）':'Baseline date (anchor)','基准日期':'Baseline date',
 '基准周':'Baseline week','基准月份':'Baseline month','基准季度':'Baseline quarter','基准年份':'Baseline year',
 '每日进出场':'Daily entries/exits','每日营收（¥）':'Daily revenue (CNY)',
 '入场车次':'Entry count','出场车次':'Exit count','营收':'Revenue','平均时长':'Average duration','峰值占用':'Peak occupancy',
 '入场':'Entry','出场':'Exit','日':'Day','周':'Week','月':'Month','季':'Quarter','年':'Year',
 '预览报表':'Preview report','报表预览':'Report preview','加载预览':'Load preview','导出 CSV':'Export CSV',
 '车辆明细':'Vehicle details','每日汇总':'Daily summary','按周汇总':'Weekly summary','按月汇总':'Monthly summary',
 '仅完整出场记录':'Complete exit records only','该周期暂无完整记录':'No complete records in this period','预览失败':'Preview failed',
 '已请求导出 PDF':'PDF export requested','已请求导出 CSV':'CSV export requested',
 // 费率
 '费率管理':'Fee rules','当前生效费率':'Current fee rule','查看历史费率':'View fee history','历史费率':'Fee history',
 '编辑当前费率':'Edit current fee rule','当前费率已保存':'Current fee rule saved','停用时间':'Ended at','创建时间':'Created at',
 '版本':'Version','暂无历史费率':'No fee history','费率加载失败':'Fee rule load failed','历史费率加载失败':'Fee history load failed',
 '免费时长（分钟）':'Free minutes (min)','首小时价格（¥）':'First-hour price (CNY)','计费单位（分钟）':'Billing unit (min)',
 '单位价格（¥）':'Unit price (CNY)','每日封顶（¥）':'Daily cap (CNY)','新能源折扣（0~1）':'EV discount (0–1)',
 '不足一个计费单位按进一法计费（round_up）':'Round up partial billing units (round_up)',
 '保存失败':'Save failed','新能源折扣应在 (0,1]':'EV discount must be in (0,1]',
 '计费单位需 ≥ 1 分钟':'Billing unit must be at least 1 minute',
 // 布局
 '车位布局':'Parking layout','版本 —':'Version —','分区汇总':'Zone summary','俯视示意图':'Top-down map',
 '声明式调整布局':'Declarative layout adjustment','车位明细':'Space details','车位号':'Space no.','区':'Zone','类型':'Type',
 '朝向':'Rotation','当前车牌':'Current plate','应用到布局':'Apply to layout','已有车位的数量与类型可调整；各区最多20位，固定场地的坐标与道具由原布局决定。':'Existing spaces can be adjusted by count/type (max 20 per zone); coordinates and props are fixed by the read-only site.',
 '应用布局变更':'Apply layout change','导入布局声明':'Import layout declaration','导入并应用':'Import and apply',
 '粘贴布局 JSON（{zones:[{zone,count,kind}]}）':'Paste layout JSON ({zones:[{zone,count,kind}]})',
 'JSON 解析失败':'JSON parse failed','缺少 zones 数组':'Missing zones array','布局加载失败':'Layout load failed',
 '应用失败':'Apply failed','导出失败':'Export failed','导入失败':'Import failed','已导出布局声明':'Layout declaration exported',
 '已更新布局':'Layout updated','已导入布局':'Layout imported',
 // 名单
 '名单管理':'Access lists','白名单 / 黑名单 / 月租车 · 影响进出场裁决':'Allowlist / blocklist / monthly · affects entry decisions',
 '新增名单':'Add list entry','名单':'List','名单类型':'List type','有效期至(本地)':'Valid until (local)','编辑名单':'Edit list entry',
 '车牌为唯一键，如需更换请删除后新增。':'Plate is the key; delete and add to replace it.',
 '有效期至（停车场本地时间，留空为长期）':'Valid until (lot local time; blank = long-term)',
 '备注（可选）':'Notes (optional)','已保存':'Saved','删除名单':'Delete list entry','确认从名单删除车牌':'Delete plate from list',
 '请输入车牌':'Enter a plate',
 // 异常
 '异常处理':'Incidents','异常与日志':'Incidents & logs','异常事件闭环处理 · 操作审计':'Incident closure · operation audit',
 '异常事件':'Incidents','操作日志':'Operation logs','操作日志（审计）':'Operation logs (audit)','异常状态':'Incident status',
 '处理':'Handle','处理说明':'Resolution notes','处理说明（如：已现场复位道闸 / 已人工核放）':'Resolution notes (e.g. barrier reset on site / manually verified)',
 '标记为已处理':'Mark resolved','已闭环':'Resolved','处理失败':'Handling failed','异常':'Incident','时间(本地)':'Time (local)',
 '时间':'Time','动作':'Action','对象':'Target','操作员':'Operator','详情':'Details',
 // 反向寻车
 '反向寻车':'Find my car','查询当前停车位置与入场照片':'Find current parking location and entry photo','完整车牌':'Full plate',
 '例如 沪UD76840':'e.g. 沪UD76840','查找车辆':'Find vehicle','输入完整车牌开始查询':'Enter a full plate to search',
 '正在查找车辆…':'Searching…','查询失败，请重试':'Search failed. Please retry.','车位位置指引':'Space location guide',
 '停车场车位图，目标车位':'Parking map, target space','北 ↑':'North ↑',
 // 识别演示
 '车牌图片识别演示':'Plate recognition demo','上传整车/车牌图片识别':'Upload a vehicle/plate image',
 '上传识别输入':'Upload recognition input','识别场景':'Recognition context',
 '点击选择 / 拖拽车牌图片到此处':'Click or drag a plate image here','上传识别结果':'Recognition results',
 '逐字符置信度':'Per-character confidence','识别输入（上传原图）':'Recognition input (uploaded image)',
 '车牌裁剪图':'Plate crop',
 '尚未识别。请上传图片，或点击左侧演示图库样例。':'Not recognized yet. Upload an image or click a demo gallery sample.',
 '演示图库':'Demo gallery',
 '识别中…':'Recognizing…','识别失败':'Recognition failed','识别输入（待识别）':'Recognition input (pending)','本地预览':'Local preview',
 '来源':'Source','合成':'Synthetic','识别状态':'Recognition status','结果说明':'Result note','综合置信':'Confidence',
 '最近识别记录（只读）':'Recent recognition logs (read-only)',
 '暂无识别记录':'No recognition logs','演示样例':'Demo sample','例如 沪UD76840':'e.g. 沪UD76840',
 // 通用错误/提示
 '网络错误：无法连接服务':'Network error: cannot reach service','网络错误':'Network error',
 '已退出登录':'Signed out','请输入':'Please enter',
 '请确认':'Please confirm','请选择 JPG/PNG 图片':'Please select a JPG/PNG image','图片不能超过 5 MiB':'Image must be ≤ 5 MiB',
 '登录尝试过多，请稍后重试':'Too many login attempts. Please retry later.','未知车牌':'Unknown plate','超时':'Timeout',
 '跟车闯入':'Tailgating','无入场记录':'No entry record','车位对账':'Space reconciliation','识别失败':'Recognition failed',
 '道闸故障':'Barrier fault','待缴费':'Payment due','摄像头故障':'Camera fault',
 // 加载失败 / 补充页面文案
 '统计数据加载失败':'Statistics load failed','费率版本加载失败':'Fee versions load failed','名单加载失败':'Access lists load failed',
 '异常事件加载失败':'Incidents load failed','操作日志加载失败':'Operation logs load failed','布局加载失败':'Layout load failed',
 '识别请求失败':'Recognition request failed','演示图库加载失败':'Demo gallery load failed','识别日志加载失败':'Recognition logs load failed',
 '页面加载失败':'Page load failed','查询失败':'Search failed',
 '颜色':'Color','置信度':'Confidence','未识别':'Unrecognized','入口识别输入':'Entry recognition input','出口识别输入':'Exit recognition input',
 '上传识别':'Upload recognition','识别结果':'Recognition results','识别状态':'Recognition status','来源：':'Source: ','合成：':'Synthetic: ',
 '候选':'Candidates','未知来源':'Unknown source','服务端裁剪图':'Server crop','服务端照片':'Server photo','上传图片':'Uploaded image',
 '停车记录':'Parking records','统计报表':'Statistics','费率管理':'Fee rules','名单管理':'Access lists','异常处理':'Incidents',
 '反向寻车':'Find my car','车牌识别':'Plate recognition',
 '识别演示':'Recognition demo','识别日志':'Recognition logs','车位数':'Spaces','闸口':'Gate',
 '免费':'Free','首时':'First hour','封顶':'Daily cap','新能源':'EV','生效时间':'Effective time',
 '识别置信度不足，转人工确认':'Low recognition confidence; manual confirmation required.',
 '📷 点击选择 / 拖拽车牌图片到此处':'📷 Click or drag a plate image here',
 '支持 JPG/PNG，≤ 5 MiB；建议使用正常角度整车图（如 EasyPR）':'JPG/PNG, ≤ 5 MiB; a normal-angle full vehicle image (e.g. EasyPR) is recommended',
 '备注：系统默认费率':'Notes: system default fee rule',
 'demo-lpr 合成演示样例':'demo-lpr synthetic sample','datasets 演示图库':'datasets demo gallery','接口/管理端上传图片':'API/admin uploaded image',
 '落杆':'Close barrier','抬杆':'Raise barrier','摄像头故障':'Camera fault','恢复':'Restore',
 '抬杆':'Raise barrier','摄像头故障':'Camera fault','恢复':'Restore',
 // 演示剧本页
 '演示剧本':'Demo scenario','编辑 3D 演示车辆编排 · 保存/重置不影响正在播放的 3D（播放时由 3D 端自行锁播放）':'Edit 3D demo choreography · saving/reset does not affect a playing scene (the 3D page locks playback itself)',
 '剧本名称':'Scenario name','随机种子 seed':'Random seed','说明':'Description','＋ 添加车辆':'＋ Add vehicle','保存剧本':'Save scenario','重置默认':'Reset default',
 '剧本元信息':'Scenario metadata','车辆编排':'Vehicle choreography','上移/下移调整播放顺序':'Move up/down to change playback order',
 '编辑说明：':'Editing notes: ','加载剧本中…':'Loading scenario…','剧本加载失败':'Scenario load failed','已加载当前剧本':'Current scenario loaded',
 '车辆数':'Vehicles','时间轴总长':'Timeline length','停车时长合计':'Total stop duration','播放顺序':'Playback order','当前列表顺序':'Current list order',
 '尚未保存':'Not saved yet','保存中…':'Saving…','保存成功':'Saved','保存失败':'Save failed','已重置默认剧本':'Reset to default scenario','重置失败':'Reset failed',
 '上移':'Move up','下移':'Move down','删除':'Delete','入场时间 t(s)':'Entry time t(s)','停车时长(s)':'Duration(s)',
 '剧本名称不能为空':'Scenario name is required','至少需要一辆演示车辆':'At least one demo vehicle is required','最多 50 辆演示车辆':'At most 50 demo vehicles',
 '剧本已保存':'Scenario saved',
 'vid 不能为空':'vid is required','vid 重复':'Duplicate vid','入场时间必须 ≥0':'Entry time must be ≥ 0','停车时长必须 >0':'Duration must be > 0',
 '车牌类型无效':'Invalid plate type','车牌需为 7/8 位中文车牌（如 湘N7D071）':'Plate must be a 7/8-character Chinese plate (e.g. 湘N7D071)',
 '车牌长度应为':'Plate length should be','实际':'actual',
 '蓝牌':'Blue plate','绿牌（新能源）':'Green plate (EV)','绿牌大车':'Large green plate','黄牌':'Yellow plate',
 '保存失败：请修正校验错误':'Save failed: please fix validation errors','当前未保存的编辑会丢失。':'Unsaved edits will be lost.',
 '重置演示剧本':'Reset demo scenario',
 '预计入场':'Expected entry','预计出场':'Expected exit','预计缴费':'Expected payment',
 '表格按 vid 唯一；plate_type 支持 blue/green/green_large/yellow；保存前会校验车牌、重复 vid、时间和时长。仅保存剧本 JSON，不启动/暂停 3D。':'The table is keyed by unique vid; plate_type supports blue/green/green_large/yellow. Plates, duplicate vids, times and durations are validated before saving. Only scenario JSON is saved; 3D is not started or paused.',
 '表格按 vid 唯一；相同车牌的时间窗不能重叠；plate_type 支持 blue/green/green_large/yellow；保存前会校验车牌、重复 vid、时间和时长。仅保存剧本 JSON，不启动/暂停 3D。':'The table is keyed by unique vid; time windows for the same plate must not overlap; plate_type supports blue/green/green_large/yellow. Plates, duplicate vids, times and durations are validated before saving. Only scenario JSON is saved; 3D is not started or paused.',
 '列表按当前顺序下发；3D 播放时由 3D 端自行锁定正在播放的剧本。':'Rows are sent in the current order; during playback the 3D page locks the playing scenario itself.',
 '蓝牌 2 · 绿牌（新能源） 2 · 绿牌大车 1 · 黄牌 1':'Blue plate 2 · Green plate (EV) 2 · Large green plate 1 · Yellow plate 1',
 // 异常处理 / 待确认 / 强制重置
 '仅异常时显示':'Shown only when abnormal',
 '设备异常':'Device faults',
 '道闸异常':'Barrier fault','摄像头异常':'Camera fault','车辆已授权，等待抬杆':'Vehicle authorized; waiting for barrier',
 '抬杆 / 落杆 / 故障 / 恢复':'Raise / lower / fault / restore','故障 / 恢复':'Fault / restore',
 '物理可通行 · 抬杆 / 落杆 / 故障 / 恢复':'Physically passable · raise / lower / fault / restore',
 '已授权，物理可通行；仍需确认业务记录':'Authorized and physically passable; verify the business record',
 '物理抬杆/落杆不代表业务授权；未结算车辆请先处理待人工确认。':'Physical raise/lower is not business authorization; settle unfinished vehicles in pending confirmation first.',
 '授权已确认，仅保留必要操作':'Authorization confirmed; only essential actions shown',
 '演示控制':'Demo controls','正常费用':'Normal fee','正常费用：查询中…':'Normal fee: loading…','正常计费':'Charge normally',
 '免费':'Free','作废':'Void','重置':'Reset','强制重置当前演示场景':'Force reset current demo scene',
 '重置将清空当前场景、未完成的开放记录与人工动作，保留已完成历史；3D 端会同步复位。确认继续？':'Reset will clear the current scene, unfinished open records and manual actions, while completed history is kept; the 3D page will be reset too. Continue?',
 '重置演示场景':'Reset demo scene','正在重置…':'Resetting…','重置完成':'Reset complete','待处理':'pending','释放车位':'released spaces',
 '重置失败':'Reset failed','重复进场':'Duplicate entry','黑名单':'Blocklist','无入场记录':'No entry record',
 '记录':'record','已授权进场':'authorized for entry','已授权出场':'authorized for exit',
 '人工授权进场':'Manual entry authorized','人工授权出场':'Manual exit authorized','人工授权被拒':'Manual authorization denied',
 '拒绝进场':'Entry denied','拒绝入场':'Entry denied','车位':'space','缴费完成':'Payment completed',
 '确认成功':'Confirmed','待办释放重试':'Pending released; retry allowed','已收费':'Charged','已免费放行':'Released free','已作废':'Voided','出口已开':'exit opened',
 '已拒绝，已释放停止线，允许重试':'Rejected; stop line released and retry allowed',
 '待办释放，允许重试':'Pending released; retry allowed',
 '识别输入（上传原图）':'Recognition input (uploaded image)',
 '出场处理方式':'Exit action','按在场记录收费结算':'Charge from on-site record','免费放行（需原因）':'Free release (reason required)',
 '异常作废（需原因）':'Void as exception (reason required)','原因（免费/作废必填）':'Reason (required for free/void)',
 '免费/作废原因（必填）':'Reason for free/void (required)','免费/作废出场必须填写原因':'Reason is required for free/void exit',
 '摄像头故障，请人工录入车牌':'Camera fault; enter plate manually','无入场记录，请选择计费、免费或作废':'No entry record; choose billing, free, or void',
 '未识别':'Unrecognized','开启中':'Opening','正常费用：—':'Normal fee: —','缴费':'payment',
 // 设备控制 / 出口异常纠正
 '设备控制':'Device controls','当前开放记录':'Open record','选择当前开放记录':'Select an open record',
 '道闸':'Barrier','摄像头':'Camera','故障成功':'Fault injected','恢复成功':'Restored',
 '正确车牌':'Correct plate','正确入场时间':'Correct entry time',
 '免费/作废原因':'Free/void reason','确认纠正':'Confirm correction','当前无开放记录，可使用免费放行或作废':'No open records; use free release or void',
 '入口车牌/时间有误或找不到记录时，选择当前开放记录并纠正；找不到记录可免费/作废。':'If the entry plate/time is wrong or no record is found, select an open record and correct it; otherwise use free release or void.',
 '请选择当前开放记录并确认纠正，或使用免费/作废':'Select an open record and confirm correction, or use free release/void',
 '未找到记录，请选择开放记录或免费/作废':'Record not found; select an open record or use free release/void',
 '开放记录加载失败':'Open records load failed','纠正失败':'Correction failed','记录已 out，出口已开':'Record is out; exit opened',
 '秒':'sec','分钟':'min'
};

// 动态内容（数量/状态前缀）规则；按顺序匹配，返回 null 表示不处理。
const PATTERNS=[
 [/^加载失败[：:]\s*(.*)$/, (m)=>`Load failed: ${translateText(m[1])}`],
 [/^(.+?)加载失败[：:]\s*(.*)$/, (m)=>`${translateText(m[1])} load failed: ${translateText(m[2])}`],
 [/^授权失败（(.+?)）：(.*)$/, (m)=>`Authorization failed (${m[1]}): ${translateText(m[2])}`],
 [/^(.+?)失败[：:]\s*(.*)$/, (m)=>`${translateText(m[1])} failed: ${translateText(m[2])}`],
 [/^第\s*(\d+)\s*行\s*(\w+)\s*车牌长度应为\s*(\d+)，实际\s*(\d+)$/, (m)=>`Row ${m[1]} ${m[2]} plate length should be ${m[3]}, actual ${m[4]}`],
 [/^(.+?)成功[：:]\s*(.*)$/, (m)=>`${translateText(m[1])} succeeded: ${translateText(m[2])}`],
 [/^共\s*(\d+)\s*条\s*·\s*第\s*(\d+)\/(\d+)\s*页$/, (m)=>`${m[1]} records · page ${m[2]}/${m[3]}`],
 [/^([A-D])\s*区$/, (m)=>`Zone ${m[1]}`],
 [/^([A-D])\s*区\s*·\s*(.+)$/, (m)=>`Zone ${m[1]} · ${translateText(m[2])}`],
 [/^空闲\s*\/\s*共\s*(\d+)$/, (m)=>`Free / ${m[1]}`],
 [/^占用\s*(\d+)（(\d+)%）$/, (m)=>`Occupied ${m[1]} (${m[2]}%)`],
 [/^版本\s*v(.+?)\s*·\s*共\s*(\d+)\s*位\s*·\s*余\s*(\d+)$/, (m)=>`Version v${m[1]} · ${m[2]} spaces · ${m[3]} free`],
 [/^空闲\s*(\d+)\s*·\s*推荐\s*(.*)$/, (m)=>`Free ${m[1]} · Recommended ${translateText(m[2])}`],
 [/^颜色[：:]\s*(.+?)\s*·\s*耗时\s*(.+?)\s*ms$/, (m)=>`Color: ${translateText(m[1])} · Time ${m[2]} ms`],
 [/^置信度\s*(.+)$/, (m)=>`Confidence ${m[1]}`],
 [/^(\d+(?:\.\d+)?)\s*分钟$/, (m)=>`${m[1]} min`],
 [/^(\d+)\s*小时(?:\s*(\d+)\s*分)?$/, (m)=>`${m[1]} h${m[2]?` ${m[2]} min`:''}`],
 [/^(\d+)\s*个分区$/, (m)=>`${m[1]} zones`],
 [/^(\d+(?:\.\d+)?)\s*折$/, (m)=>`${(Number(m[1])*10).toFixed(0)}% of price`],
 [/^推荐车位：蓝牌\s*(.*?)\s*·\s*绿牌\(新能源\)\s*(.*)$/, (m)=>`Recommended: blue ${m[1]} · green (EV) ${m[2]}`],
 [/^已关闭会话\s*(.*?)，释放\s*(\d+)\s*个车位$/, (m)=>`Closed session ${m[1]}, released ${m[2]} spaces`],
 [/^(.+?)\s*·\s*车辆\s*(.+?)\s*·\s*重试\s*(\d+)$/, (m)=>`${translateText(m[1])} · vehicle ${m[2]} · retry ${m[3]}`],
 [/^(.+?)\s*·\s*置信\s*([\d.]+)$/, (m)=>`${translateText(m[1])} · confidence ${m[2]}`],
 [/^入场时间[：:]\s*(.+)$/, (m)=>`Entry time: ${m[1]}`],
 [/^车位\s*(.+)$/, (m)=>`Space ${m[1]}`],
 [/^([⬇⬆＋])\s*(.+)$/, (m)=>`${m[1]} ${translateText(m[2])}`],
 [/^📷\s*(.+)$/, (m)=>`📷 ${translateText(m[1])}`],
 [/^v([\d.]+)\s*·\s*无构建原生 ES$/, (m)=>`v${m[1]} · No-build native ES`],
 [/^\/\s*(\d+)\s*空闲$/, (m)=>`/ ${m[1]} free`],
 [/^备注[：:]\s*(.+)$/, (m)=>`Notes: ${m[1]}`],
 [/^周期[：:]\s*(.+?)\s*·\s*区间\s*(.+?)\s*~\s*(.+?)（停车场本地时间）\s*·\s*生成于\s*(.+)$/, (m)=>`Period: ${m[1]} · Range ${m[2]} ~ ${m[3]} (lot local time) · Generated ${m[4]}`],
 [/^CN¥(.+?)\s*\/\s*(\d+)\s*分钟$/, (m)=>`CN¥${m[1]} / ${m[2]} min`],
 [/^网络错误[：:]\s*无法连接服务（(.*)）$/, (m)=>`Network error: cannot reach service (${m[1]})`],
 [/^(入口|出口)识别输入$/, (m)=>`${m[1]==='入口'?'Entry':'Exit'} recognition input`],
 [/^来源[：:]\s*(.+?)\s*·\s*合成[：:]\s*([^·]+?)\s*(?:·.*)?$/, (m)=>`Source: ${translateText(m[1])} · Synthetic: ${translateText(m[2])}`],
 [/^车牌\s+(.+?)\s*·\s*置信度\s*(.+)$/, (m)=>`Plate ${m[1]} · Confidence ${m[2]}`],
 [/^版本\s*v([\d.]+)\s*·\s*无构建原生 ES$/, (m)=>`v${m[1]} · No-build native ES`],
 [/^(入口|出口)场景\s*\((in|out)\)$/, (m)=>`${m[1]==='入口'?'Entry':'Exit'} context (${m[2]})`],
 [/^(日|周|月|季|年)统计$/, (m)=>({日:'Day',周:'Week',月:'Month',季:'Quarter',年:'Year'}[m[1]] + ' statistics')],
 [/^(日|周|月|季|年)$/, (m)=>({日:'Day',周:'Week',月:'Month',季:'Quarter',年:'Year'}[m[1]])],
 [/^(.+?)·\s*置信度\s*(.+)$/, (m)=>`${translateText(m[1])} · Confidence ${m[2]}`],
 [/^(.+?)·\s*置信\s*([\d.]+)$/, (m)=>`${translateText(m[1])} · Confidence ${m[2]}`],
 [/^(.+?)·(入口|出口)$/, (m)=>`${translateText(m[1])} · ${m[2]==='入口'?'Entry':'Exit'}`],
 [/^#(\d+)\s+(入口|出口)$/, (m)=>`#${m[1]} ${m[2]==='入口'?'Entry':'Exit'}`],
 [/^图像源不可用，请人工确认$/, ()=>'Image source unavailable; manual confirmation required'],
 [/^说明[：:]\s*(.+)$/, (m)=>`Note: ${translateText(m[1])}`],
 [/^第\s*(\d+)\s*行与第\s*(\d+)\s*行车牌\s*(.+?)\s*时间窗重叠（(.+?)）$/, (m)=>`Row ${m[1]} overlaps row ${m[2]} for plate ${m[3]} in time window (${m[4]})`],
 [/^同一车牌\s*(.+?)\s*的入场时段重叠[：:]\s*(.*)$/, (m)=>`Overlapping entry windows for plate ${m[1]}: ${m[2]}`],
 [/^(.*?)\s*·\s*记录\s*#(\d+)(.*)$/, (m)=>`${m[1]} · record #${m[2]}${m[3]}`],
 [/^人工授权进场[：:]\s*(.+)$/, (m)=>`Manual entry authorized: ${m[1]}`],
 [/^人工授权出场[：:]\s*(.+)$/, (m)=>`Manual exit authorized: ${m[1]}`],
 [/^人工授权被拒[：:]\s*(.+)$/, (m)=>`Manual authorization denied: ${translateText(m[1])}`],
 [/^人工授权进场\s+(.+?)\s*→\s*(.+?)（(.+?)）$/, (m)=>`Manual entry authorized ${m[1]} → ${m[2]} (${m[3]})`],
 [/^人工授权出场\s+(.+?)\s*·\s*(.+)$/, (m)=>`Manual exit authorized ${m[1]} · ${m[2]}`],
 [/^拒绝入场\s+(.+?)\s*·\s*(.+)$/, (m)=>`Entry denied ${m[1]} · ${translateText(m[2])}`],
 [/^车牌\s*(.+?)\s*已有未出场记录$/, (m)=>`Plate ${m[1]} already has an open record`],
 [/^车牌\s*(.+?)\s*无在场记录$/, (m)=>`No on-site record for plate ${m[1]}`],
 [/^车牌\s*(.+?)\s*无入场记录$/, (m)=>`No entry record for plate ${m[1]}`],
 [/^车牌\s*(.+?)\s*已在场$/, (m)=>`Plate ${m[1]} is already on site`],
 [/^车牌\s*(.+?)\s*在黑名单中.*$/, (m)=>`Plate ${m[1]} is on the blocklist`],
 [/^长度应为\s*(\d+)，实际\s*(\d+)$/, (m)=>`Length should be ${m[1]}, actual ${m[2]}`],
 [/^车牌格式无效[：:]\s*(.*)$/, (m)=>`Invalid plate format: ${m[1]}`],
 [/^(.+?)\s*已授权进场$/, (m)=>`${m[1]} authorized for entry`],
 [/^(.+?)\s*已授权出场$/, (m)=>`${m[1]} authorized for exit`],
 [/^(.*?)\s*·\s*记录\s*#(\d+)$/, (m)=>`${m[1]} · record #${m[2]}`],
 [/^第\s*(\d+)\s*行\s*(.+)$/, (m)=>`Row ${m[1]}: ${translateText(m[2])}`],
 [/^重置完成：作废\s*(\d+)\s*·\s*待处理\s*(\d+)\s*·\s*释放车位\s*(\d+)$/, (m)=>`Reset complete: aborted ${m[1]} · pending ${m[2]} · released spaces ${m[3]}`],
 [/^(进场|出场)授权成功：(.+)$/, (m)=>`${m[1]==='进场'?'Entry':'Exit'} authorized: ${m[2]}`],
 [/^(进场|出场)授权失败：(.+)$/, (m)=>`${m[1]==='进场'?'Entry':'Exit'} authorization failed: ${translateText(m[2])}`],
 [/^蓝牌\s*(\d+)\s*·\s*绿牌（新能源）\s*(\d+)\s*·\s*绿牌大车\s*(\d+)\s*·\s*黄牌\s*(\d+)$/, (m)=>`Blue plate ${m[1]} · Green plate (EV) ${m[2]} · Large green plate ${m[3]} · Yellow plate ${m[4]}`],
 [/^vid 重复[：:]\s*(.+)$/, (m)=>`Duplicate vid: ${m[1]}`],
 [/^·\s*(.+)$/, (m)=>`· ${translateText(m[1])}`],
 [/^(.*?)\s*置信\s*([\d.]+)$/, (m)=>`${translateText(m[1])} confidence ${m[2]}`],
 [/^道闸异常\s*·\s*(入口|出口)$/, (m)=>`Barrier fault · ${m[1]==='入口'?'Entry':'Exit'}`],
 [/^摄像头异常\s*·\s*(入口|出口)$/, (m)=>`Camera fault · ${m[1]==='入口'?'Entry':'Exit'}`],
 [/^车辆已授权，等待抬杆\s*·\s*(入口|出口)$/, (m)=>`Vehicle authorized; waiting for barrier · ${m[1]==='入口'?'Entry':'Exit'}`],
 [/^正常费用：¥([\d.]+)\s*·\s*(\d+)\s*分钟$/, (m)=>`Normal fee: ¥${m[1]} · ${m[2]} min`],
 [/^正常费用：(.+?)\s*·\s*(\d+)\s*分钟$/, (m)=>`Normal fee: ${m[1]} · ${m[2]} min`],
 [/^(.+?)\s*·\s*重试\s*(\d+)$/, (m)=>`${translateText(m[1])} · retry ${m[2]}`],
 [/^\[(.+?)\]\s*→\s*(.+)$/, (m)=>`[${m[1]}] → ${translateText(m[2])}`],
 [/^#(\d+)\s*\[(.+?)\]\s*未识别\s*·\s*(.+)$/, (m)=>`#${m[1]} [${m[2]}] Unrecognized · ${translateText(m[3])}`],
 [/^已收费\s*(.+)，出口已开$/, (m)=>`Charged ${m[1]}; exit opened`],
 [/^已收费 (.+)，记录已 out，出口已开$/, (m)=>`Charged ${m[1]}; record out; exit opened`],
 [/^已收费 (.+) · 记录已 out，出口已开$/, (m)=>`Charged ${m[1]} · record out; exit opened`],
 [/^Charged (.+)，记录已 out; exit opened$/, (m)=>`Charged ${m[1]}; record out; exit opened`],
 [/^Charged (.+)，记录已 out，出口已开$/, (m)=>`Charged ${m[1]}; record out; exit opened`],
 [/^Charged (.+) · 记录已 out; exit opened$/, (m)=>`Charged ${m[1]} · record out; exit opened`],
 [/^已免费放行，出口已开$/, ()=>'Released free; exit opened'],
 [/^已作废，出口已开$/, ()=>'Voided; exit opened'],
 [/^已结算，出口已开$/, ()=>'Settled; exit opened'],
 [/^(.+?)\s*缴费\s*(cash|free|void|charge)(.*)$/, (m)=>`${translateText(m[1])} payment ${m[2]}${m[3]}`],
 [/^缴费\s*(cash|free|void|charge)(.*)$/, (m)=>`payment ${m[1]}${m[2]}`],
 [/^(.+?)（([A-D])区）$/, (m)=>`${translateText(m[1])} (Zone ${m[2]})`],
 [/^待办释放，允许重试\s*·\s*(.+)$/, (m)=>`Pending released; retry allowed · ${translateText(m[1])}`],
];

function translateCore(core) {
 if (!core) return core;
 if (PHRASES[core]) return PHRASES[core];
 for (const [re, fn] of PATTERNS) {
   const m = core.match(re);
   if (m) return fn(m);
 }
 return core;
}

export function translateText(value) {
 if (locale !== 'en' || value == null) return value;
 const raw = String(value);
 if (!raw.trim()) return raw;
 const m = raw.match(/^(\s*)([\s\S]*?)(\s*)$/);
 if (!m) return raw;
 return m[1] + translateCore(m[2]) + m[3];
}

const SKIP_TAGS = new Set(['SCRIPT','STYLE','TEXTAREA','INPUT','CODE','PRE']);
const ATTRS = ['placeholder','title','aria-label','alt'];

export function translateDom(root = document.body) {
 if (locale !== 'en' || !root) return;
 const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
   acceptNode(node) {
     const parent = node.parentElement;
     if (!parent || SKIP_TAGS.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
     if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
     return NodeFilter.FILTER_ACCEPT;
   },
 });
 const nodes = [];
 while (walker.nextNode()) nodes.push(walker.currentNode);
 for (const node of nodes) {
   const next = translateText(node.nodeValue);
   if (next !== node.nodeValue) node.nodeValue = next;
 }
 root.querySelectorAll?.('*').forEach((node) => {
   if (!node.getAttribute || node.tagName === 'SCRIPT' || node.tagName === 'STYLE') return;
   for (const attr of ATTRS) {
     const old = node.getAttribute(attr);
     if (!old) continue;
     const next = translateText(old);
     if (next !== old) node.setAttribute(attr, next);
   }
 });
}

let observer = null;
let scheduled = false;
export function startI18nObserver(root = document.body) {
 if (locale !== 'en') return () => {};
 translateDom(root);
 if (observer) observer.disconnect();
 observer = new MutationObserver(() => {
   if (scheduled) return;
   scheduled = true;
   queueMicrotask(() => {
     scheduled = false;
     translateDom(root);
   });
 });
 observer.observe(root, { childList: true, subtree: true, characterData: true, attributes: true,
   attributeFilter: ['placeholder','title','aria-label','alt'] });
 return () => { if (observer) { observer.disconnect(); observer = null; } };
}

export function setLocale(value){
 if (typeof localStorage !== 'undefined') localStorage.setItem('parking.admin.locale', value === 'en' ? 'en' : 'zh-CN');
 if (typeof location !== 'undefined') location.reload();
}

// 测试/调试：导出短语数量，便于 Node 冒烟断言。
export const _phraseCount = Object.keys(PHRASES).length;
