> 当前执行范围：用户在本轮明确要求暂不以真实照片≥70%阻塞，优先指定开源字形的绿牌与干扰样式基准。本轮未重训；以下比例为原始目标，实拍PR-2仍未验收。当前生成器/便携字形及v2测试详见 ../../07-识别数据与复现.md。

# T2 数据方案

## 来源与许可

1. 首选 CCPD 官方 2019 数据及 CCPD-Green：<https://github.com/detectRecog/CCPD>。官方 README 声明 MIT；原始数据下载在 Google Drive 与百度网盘。下载后逐项保留官方 LICENSE、版本、下载时间、原始包 SHA-256、原始相对路径和论文引用。CCPD 的文件名包含车牌框、四角点、字符索引、亮度和模糊度。CCPD 主要为整车/道路场景；实际比例须在下载后人工抽样核验，不能凭数据集名称推断。
2. 独立近景真实照片：优先用户提供的明确授权照片或许可清晰的公开数据。必须核对原始摄影场景、号牌真实性、许可范围、拍摄角度和与 CCPD 的重复关系；目前 **0 张已核验**，来源待落实。
3. 合成辅助数据：只在上述真实数据足量且比例核验后加入训练。参考 <https://github.com/Pengfei8324/chinese_license_plate_generator> 的 `font_model/` 字形、`plate_model/` 底板和车牌版式，附 MIT LICENSE 与署名。旧 `server/lpr/render.py` 的系统字体/自定底板不能作为新训练集生成器。不得使用生成图作真实测试样本。固定素材提交 `df4757f99e63d7180a59d6c2633f466a339b500b`；仓库 LICENSE 为 `MIT License, Copyright (c) 2020 leijd`。
4. 候选补充来源：<https://github.com/treeandgrass/License-plate-data-set>，固定提交 `dec642ea4471b8f22347c4ee13a52fb1a212109f`，仓库 MIT LICENSE 已与本机下载包一起保存。README 称有 1205 张中国车牌图，实际压缩包有 1223 张 JPG/JPEG；其中 1222 张可解码、1 张损坏、按原图 SHA-256 去重后 1201 张。抽样确认存在道路整车照和近距离车牌照，但仓库未说明每张照片的拍摄来源，也不提供框/四角点；文件名车牌还需人工复核。因此当前全部标为候选，不计入已审核训练集或 PR-2 测试集。

## 最低数据门槛

- 每张样本记录：`sample_id`、原始文件 SHA-256、来源/许可、`real|synthetic`、拍摄类型 `closeup|vehicle`、车牌字符串、颜色/类型、矩形框、四角点、角度/光照/模糊/污损/遮挡标签及标注审核状态。
- 车牌框和四角点用原图像素坐标；字符按大陆普通/新能源号牌规则规范化，保留原始标签及修订历史。无法核实的标签不进入训练或测试。
- 训练样本中真实照片占比 `N_real/(N_real+N_synthetic) >= 0.70`，目标 `>= 0.80`。训练和验证分别报告这个比例；测试集 `real=100%`。比例按**原始照片**计，不把同源裁剪、旋转或增强当作新的真实拍摄张数。
- 真实训练集同时包含不同角度号牌近景和含号牌整车照片，覆盖正视、侧倾、俯仰、远近、日夜/逆光、蓝/绿牌。每个桶给出实际数量；不足的桶明确为缺口，不用复制增强凑数。
- 处理个人信息：仅为训练/评测保存受许可的图像；限制原图访问，报告仅展示授权或脱敏样例；不把真实车牌号和原图上传到外部服务。

## 切分与防泄漏

- 固定 train/val/test 清单与随机种子；按车牌号、摄影序列和原始图像来源分组切分。同一车牌、同一连续拍摄序列及其任何裁剪/增强只能处于一个 split。
- CCPD 官方 Base train/val 与六个挑战测试子集作为来源参考；若与自采图混用，先做精确 hash、感知 hash 和号牌/场景去重。测试清单冻结后不参与阈值调参、模板生成或模型选择。
- 若使用 CCPD-Green，单独检查八位新能源牌、颜色标注与官方分割列表；不把它自动混入旧七位规则。

## 合成版式边界

蓝牌物理尺寸 440x140 mm，新能源绿牌 480x140 mm；按照有许可的底板/字形和适用规范处理蓝、绿、黄、白、黑牌。普通牌结构为省份简称 + 发牌机关字母 + 间隔点 + 5 位序号；新能源位数和字符位置单独建规则。边距、铆钉、反光、透视、污损等参数必须与真实照片统计对照并记录生成脚本、版本和种子。生成物只用于训练辅助。

## 当前获取状态（2026-10-20）

| 数据 | 已核验可用 | 说明 |
| --- | ---: | --- |
| 输入包 `datasets/plates` | 0 张真实 | 160 张项目合成图；仅保留旧实现回归用途 |
| CCPD 官方完整包 | 0 张 | 已核验 README、MIT、标注和网盘链接，未获取图像 |
| CCPD 仓库演示图 | 5 张待核验 | 数量和标注不足，不能形成训练/评测集 |
| 独立号牌近景实拍 | 0 张 | 待取得授权和标签 |
| EasyPR GDTS `general_test` | 256 张来源照片 | GDSL-0.1 非商业学习用途；249 张当前普通号牌规则可用，图像内容/类别仍需逐张审计；无真实四角点与新能源覆盖 |
| treeandgrass 公开图包 | 1201 张去重候选 | MIT 仓库；1222 张可解码，1 张损坏，21 个重复文件；有整车/近景样例，但来源、标签、分组、框及四角点未审核 |
| 指定生成器蓝/绿牌辅助图 | 40 张合成 | `tools/generate_reference_plates.py --per-color 20 --seed 42`；仅作版式/后续增广候选，不计入实拍、训练或 PR-2 测试 |

官方 CCPD/CCPD-Green 的 Google Drive 下载入口在当前环境均返回 HTTP 403。GitHub 检索到的 DGUT_LPR README 明确说明图片由 CycleGAN 生成，CBLPRD-330k README 明确说明由 GAN 生成；两者都不能计入真实照片比例。Hugging Face 目录连接被重置。这些失败和排除项不计入可用数据。

结构化导入命令：`python tools/prepare_easypr.py`，输出 `datasets/real/easypr-manifest.json`（不入 Git，图片保留在 `datasets/external/EasyPR`）。XML 声明 ISO-8859-1 但内容为 UTF-8，导入器显式按 UTF-8 解析；两条相同重复标注去重，两条缺图记录保留在清单中。每个样本记录原图 SHA-256、主车牌框、来源和按车牌号哈希划分的 split。官方没有四角点、拍摄类别和清晰污损标签；导入器不臆造这些字段。

treeandgrass 原包下载至 `datasets/external/treeandgrass-license-plates.zip`（SHA-256 `210bf81985bad271e5d8a8132bce742b9243ed93ae9174cf80f7a5ca8d42d4b1`），同目录下 `treeandgrass/LICENSE` 的 SHA-256 为 `83e4dd21429a91fb7cea67a476032a9641425e5355df2e0f589a738b6ec9fd2c`。运行 `python tools/prepare_treeandgrass.py` 校验两个哈希、解码中文文件名和图像、拒绝越界路径，输出忽略目录中的 `datasets/real/treeandgrass-manifest.json`。清单仅保存未核验文件名标签、尺寸及哈希，`plate`、bbox、四角点和 split 保持空值。照片来源不明和无定位标注是进入训练前必须解决的限制。

合成辅助图使用指定仓库原始 `font_model/` 字形和 `plate_model/blue_140.PNG`、`green_car_140.PNG` 底板，由其 `MultiPlateGenerator.generate_plate_special` 渲染。蓝牌 440×140 mm、绿牌 480×140 mm，在辅助图中以 1 px/mm 表示；蓝牌底板自带实体间隔点，字符标签以 `省份+机关·序号` 展示，模型标签不含点。固定种子 42、每色 20 张、无增强/透视/污损；输出 `datasets/external/reference-synthetic/` 的 PNG 与 `manifest.json`（均不入 Git），逐张 SHA-256、底板/脚本/许可 SHA-256 和准确的全图框/角点可复核。脚本拒绝素材提交或工作树变更。**清单标记 `source_kind=synthetic`、`training_eligible=false`、`pr2_eligible=false`**，不会混入实拍测试。如果未来使用全部 40 张辅助图，至少需要 94 张审核通过的真实训练原图才能达到 70%，目标 80% 则需 160 张；现有 EasyPR `train=167` 尚未完成拍摄类别/角度/四角点审核，因此不能直接宣称已达到训练比例。

复现：`git clone --depth 1 https://github.com/Pengfei8324/chinese_license_plate_generator.git datasets/external/chinese_license_plate_generator`；确认 HEAD 为上述固定提交，再运行 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tools/generate_reference_plates.py --per-color 20 --seed 42`。本次输出清单 SHA-256 为 `a957fa7c44753bc4998e92901e3ee72facbc2500d3f596eb44c3deeb31fb40b2`，许可文件 SHA-256 为 `04ed9869b4ae0aac581beed825697274091352cf58c58f928979358eb6829256`。脚本不会下载素材，也不会自动把生成图纳入训练。

额外公开来源核查：`huangxinping/chinese_license_plates` 虽为 MIT，但文件树主体是生成器字形、底板，只有 6 张样例，不能当实拍集；蓝绿牌预览仓库 `lonlonago/Domestic-Vehicle-License-Plates-Blue-and-Green-Labels-for-New-Energy-and-Oil-Cars-Detection-7a7cc9cd` 无仓库许可且只有 3 张预览图，未纳入。GitHub 公共仓库目录和上述文件树已于 2026-10-20 核对；Hugging Face API 在当前网络重置连接。

补充公开搜索核查（2026-10-20）：`xiaosongshine/CCPD_Plus` 为 MIT，但仓库仅有一张黄牌示例图、两份对应标注与脚本；README 的 CCPD/CCPD-Green 数据链接仍指向已核查的 Google Drive/百度网盘，未获得新的实拍绿牌图包。`byu014/green_energy_lp` 的仓库目录主要为绿牌生成器和演示输出，不计入真实照片。GitHub 仓库搜索 `CCPD-Green license plate dataset` 与 `chinese green license plate dataset` 均无直接命中；泛化搜索命中的生成器、非中国号牌项目和缺许可项目未纳入训练或评测。

**结论：已得到一批有许可的实拍来源，但真实训练比例、近景/整车/绿牌与 PR-2 分组门槛仍未通过。**
