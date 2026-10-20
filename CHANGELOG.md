# Changelog

本项目所有值得记录的变更都会写在这里，格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [1.5.1] - 2026-10-20 - 首次上线 CI 后抓到的修复

> 1.5.0 发布后接上 GitHub Actions，第一次跑就红了三个 job。
> 下面这些是真 bug，不是测试写错——尤其第一条会影响所有干净环境的用户。

### 修复

- **桌面端漏声明 Pillow 依赖**（会影响真实用户）。
  `desktop/app/engine/embed.py` 写 PNG tEXt 元数据、`runner` 保存帧都 `import PIL`，
  但 `requirements.txt` 里没有 Pillow。Windows 上因为 ComfyUI 环境自带 Pillow，
  这个问题一直没暴露；**任何干净环境（Linux/macOS/精简 venv）一保存就
  `No module named 'PIL'`**。已补 `Pillow>=10.0`。
- `tests/test_keyvault.py` 里 `test_resolve_api_key_ignores_label` 注释写着
  「用临时配置文件」，实际读的是真实 `config.ini`——只在作者机器上通过。
  改为打桩，与机器无关。
- `tests_ui.py` 的自检断言要求「本机装好 ComfyUI + VAE 模型」，CI 上不可能满足。
  改为只守两条真不变量：失败项必须带修复建议、仓库布局下插件目录必须能探到。

### 改进（测试稳定性）

- 三个 Qt 相关套件加 `faulthandler` 与显式收尾
  （停线程 → 关窗口 → processEvents → `app.quit()`）。
  headless Linux 上曾出现退出码 139（SIGSEGV）且无 traceback，现在能打出栈。
- 测试脚本去掉硬编码的 `C://AI//dongshan//...` 路径，任何机器 clone 下来都能跑。
- 每个测试文件独立成 CI 步骤，失败点无需登录即可定位。

### 工程

- CI 失败时用 `::error::` annotation 输出尾部日志——公开仓库无需凭据即可排障。
- `.gitignore` 补上本地排障用的临时抓取文件。

## [1.5.0] - 2026-10-20 - 桌面出图器可用性补齐 + CI

> 插件本身没有破坏性变更；这一版主要是把 `desktop/` 从「能出图」推到「敢给别人用」。

### 新增（桌面出图器）

- **启动自检**：ComfyUI 路径 / 插件目录 / VAE 模型 / ffmpeg / API Key / 输出目录一次查完，
  缺哪项直接在对话框里给修复建议，并可一键打开设置。新用户不用再挨个踩。
- **任务台账查看器**：菜单 →「任务台账」，列出时间 / 任务号 / 结果 / 积分 / 耗时 / 备注，
  可直达 jsonl 文件。数据本来就在写，这次终于有界面了。
- **每日积分上限**：设置里可填上限（0 = 不限制）。提交前按台账统计当日消耗，
  超限先弹窗确认——防手滑连点。
- **系统托盘 + 完成通知**：出视频要等几分钟，窗口最小化后完成 / 失败会弹系统通知，
  托盘菜单可恢复窗口或退出。
- **按 taskId 续取**：超时、断网、误关窗口都能把已付费的云端结果捞回来，
  不重新提交、不重复扣费。复用「轮询之后」的全部阶段，规则与正常流程完全一致。
- **设置里「从插件 config.ini 导入 Key」**：想省事可以一键把 ComfyUI 里配好的 Key
  抄过来。显式按钮，程序本身**不会**去读别的应用的配置。

### 改进

- **进度文案全部走 i18n**：引擎层不再硬编码中文，改为向上报 key、由界面翻译
  （英文界面下状态栏不再是中文）。解码器的 8 条阶段提示一并改造。
- **`GenerationRunner.run()` 拆分**：277 行的巨型方法拆成
  `_prepare / _submit / _poll / _fetch_samples / _decode_and_* / _finalize / _write_ledger`
  八个阶段方法，新增 `resume()` 复用后半段。行为不变。
- 作品栏固定单元格尺寸（`setGridSize`）：修掉长文件名导致的缩略图与文字重叠。
- 测试脚本去掉硬编码绝对路径，任何机器 clone 下来都能直接跑。

### 修复

- **取消被记成「失败」**：台账里 `outcome` 现在正确区分为 `canceled` 与 `failed`
  （对账时这两者含义完全不同）。
- 台账补记 `coins_spent`（本次实际消耗），此前只有 `coins_before`，无法直接统计。
- 设置里「浏览」按钮的 `only_dirs` 参数是假的（两个分支代码相同），已收敛。

### 工程

- 新增 `tests_runner.py`：**首次给核心编排补上测试**。用假客户端跑完
  提交 → 轮询 → 取回 → 解码 → 落盘，覆盖正常出图 / 超时续等 / 取消 / 预校验拦截 /
  空输出五条路径（8 个用例）。
- 新增 `tests_ui.py`：自检、台账、成本护栏、设置项（10 个用例）。
- 新增 `.github/workflows/tests.yml`：插件 5 个套件（py3.10/3.12）+ 桌面 6 个离线套件。
- 新增 `tools/build_release.py`：一条命令出发布包（排除敏感物 → 扫本机 Key →
  校验必需文件 → 打 zip）。
- `desktop/` 纳入本仓库，与插件共用同一份 `core/`。

## [1.4.0] - 2026-10-20 - 首次公开发布

> 首次以开源形式发布。包含从 1.3.1 以来的四轮改造，
> **升级前请先读下面的「升级须知」**。

### 升级须知（从 1.3.x 升上来的人看这段）

1. **删除了 3 个本地 AV 节点**：`RH AV Latent 拆分 / 合并 / 保存`。
   旧工作流里还挂着它们的位置会显示「缺少节点类型」，删掉节点重新连线即可。
2. **`api_key` 输入框的语义变了**：以前是密码框（存真 Key），现在是状态指示器
   （显示 `已配置 · 使用 config.ini`）。Key 只存在 `config.ini`，
   已保存在旧工作流里的明文 Key 会在打开时自动转存并清空。
3. **提示词不再自动猜落点**：必须用 `{positive}` / `{negative}` 占位符，
   或在参数映射里显式指定。填了提示词却没找到落点会**直接报错**（不再静默跑完）。

### 新增

- **API Key 守卫：Key 不进工作流 / 图片，但自己不用反复填**。三层防护：
  1. 存储层——Key 只存在插件目录 `config.ini`。推荐用「右键节点 → 设置 API Key」
     写入（新增本地接口 `POST /runninghub_latent/save_key`、
     `GET /runninghub_latent/key_status`，后者只返回「是否已配置 + 头尾 4 位脱敏」）；
     直接粘在输入框里的真 Key 会在失焦时被转存并把框换回状态标签；
  2. 序列化层——前端在所有出口（`graphToPrompt`：队列提交 / PNG 元数据 /
     导出 API 格式；`graph.serialize`：保存工作流 JSON / 剪贴板）统一清空
     `api_key` 字段，别人拿到你的工作流或图片打开就是空的；
  3. 显示层——输入框里放的是**无害状态标签**（`已配置 · 使用 config.ini`），
     由服务端 `INPUT_TYPES` 生成，重启 / 重载工作流后依然是这个状态，
     不会再出现「一片空白以为 Key 丢了」。标签不是密钥，序列化也天然安全。
  执行时节点把标签当「未填」，回落 config.ini，行为不变。
  设计取舍（以及为什么放弃「视图层遮罩圆点」）见 `docs/key-handling.md`。
  注意：若把 Key 通过连线从其他节点接入，该节点的值不受守卫保护，请勿这样接。
- **report 输出积分消耗与整体用时**：任务前后各查一次
  `/uc/openapi/accountStatus`，report 里直接写「本次积分消耗：约 N」
  与「整体用时：N 秒」。期间有并发任务导致差值为负时会明确标注仅供参考。
  可用 config.ini `query_coins = false` 关闭；查询失败绝不影响任务本身。
- **任务台账（`core/ledger.py`）**：每次云端调用追加一行 JSON 到
  `output/runninghub_ledger.jsonl`，记录 taskId / 工作流 / 结果 / 积分 /
  用时；失败任务也记（含分类与原因），方便对账与「按任务号取回」时翻历史。
- **「RH 缓存管理」节点**：查看缓存占用 / 最近 10 条台账，或一键清理缓存。
- **提交前映射预校验**：参数映射指向云端不存在的 nodeId / fieldName 时
  本地直接报错（附带明细），不再花钱跑云端收 803；连线字段仍为警告。
- **轮询超时自动续等**：新增 `timeout_retries`（专业版 / 取回节点有 widget，
  极简版走 config.ini，默认 1）。排队超过 timeout 时自动再等一轮，
  已付费的任务不再被本地计时器判死。
- **api_key 输入框改密码样式**（INPUT_TYPES `password` + 前端兜底），
  防止截图 / 演示时泄漏 Key。

### 移除（用户拍板的瘦身，共约 2200 行）

- 删除「RH AV Latent 拆分 / 合并 / 保存」三个本地节点：单文件
  「MiniMax H3 Save AV Latent」方案定稿后，本地只负责解析复合 latent，
  不再需要本地 AV 节点（已保存工作流里若还挂着这三个节点会显示为缺失，
  删除节点换线即可）。
- 删除从未接入运行时的 S2 层：`core/binding.py`、`core/snapshot.py`
  及其测试；一次性调研工具 `tools/av_latent_unpack.py`、`extras/` 目录；
  零引用函数 `client.get_workflow_params`、`client._TERMINAL_CODES`、
  `latent.load_safetensors_av`、`latent.save_av_latent`。
- 前端移除「警告染黄 / 报错染红」节点上色（用户反馈不喜欢），诊断只走
  toast；错误码文案收敛到 `errors.CODE_HINTS` 单一出口。

### 修复

- `_run_sampling` 内局部函数遮蔽模块级 `_stage` 导致的
  `UnboundLocalError`（真实炸过一次）；新增 AST 静态检查防复发。

### 本轮配套改造：报错机制细化（不查日志也能判断卡在哪一步）

### 新增：`core/errors.py` 统一错误分类与诊断文案

以前每个环节各抛各的异常，落到 ComfyUI 前端只剩一句「节点在执行过程中发生错误」，
用户只能翻完整日志。现在所有失败统一走 `RHUserError.build()`，四段式输出：

```
<节点名>执行失败
问题类型：…        失败环节：…
原因：…
<关键上下文：出错的节点 / 字段 / 错误码 / 服务端原话>
怎么排查：1… 2… 3…
```

分类依据是「用户下一步要做什么」，不是异常的技术类型：
认证 / 工作流 / 参数映射 / 必填项 / 额度并发 / 网络超时 / 返回体解析 /
云端执行 / 下载 / latent 解析 / 本地环境。

### 各节点改动

- **所有调 API 的节点**（极简版、专业版、按任务号取回、从 URL 加载、参数探查）
  入口套 `_guarded`：任何异常都兜底成带分类的中文诊断，并连同原始类型一起回显。
- **关键步骤套 `_stage`**：读配置 / 拉云端结构 / 提交任务 / 轮询等待 /
  下载 latent / 解析 latent / 保存文件，报错里明确写出卡在哪一环。
- **`core/client.py`**：网络层拆成超时 / 连接失败 / SSL 三类；HTTP 层按状态码
  归类（401/403 认证、402/429 额度、404 路径、5xx 服务端）；解析层单独报
  「返回体不是 JSON」并附上原始返回。带 `retryable` 标记——只有网络问题和 5xx
  会重试，4xx 不再做无意义的重试。
- **`failedReason` 兼容字符串形态**：平台直接给字符串时早先会抛
  `AttributeError: 'str' object has no attribute 'get'`，把真正的云端报错吞掉；
  现在 dict / str / None 三种形态都能带出云端节点名与原始报错。
- **`core/cache.py` 下载失败分类**：404/403 说清「链接过期只能重跑」，
  超时/断连说清「查代理」，5xx 可重试。
- **诊断文案密钥脱敏下沉到渲染层**：任何上下文值在输出前统一打码。

### 行为变化（需要注意）

- **多个尺寸不同的 latent 从「取第一个」改为直接报错**。这是静默丢数据的老毛病：
  MiniMax H3 用户就是这样把视频 latent 当成音频 latent 送去解码的。
- **参数探查节点**拉不到结构时不再只给通用建议，会带上真实的错误码与原因。
- **提示词落点红线**的示例不再写死 `"fieldName": "text"`，
  改用云端真实字段名（可能是 `prompt` / `value` / `string`）。
- **前端 `web/rh_warning.js`** 新增报错通道：节点变红 + 常驻红色 toast。
  因为 ComfyUI 新版前端会把节点异常统一显示成「节点在执行过程中发生错误」，
  自定义文案在那里出不来，只能自己开一条通道送到画布上。

---

### 本轮配套改造：推倒自动匹配提示词，改为纯手动映射

用户拍板：「图像大小设置和提示词应用每条工作流都不一样，手动靠谱」。

### 删除：沿连线回溯的提示词兜底

`core/workflow.py` 的 `_upstream_text_source` / `_linked_prompt_targets` 已删除，
`build_prompt_injections` 现在**只认占位符**，不再沿采样器的 positive/negative
连线向上游找文本字段。理由：猜出来的落点一旦错了就是静默失效（用户以为生效了，
其实没送上去），而「上游唯一字段即采用」正是架构文档 §3.5 明令禁止的拓扑假设
（S0 fixture c5 就是反例：唯一字段其实是模板前缀 `prefix`）。

保留不变：`{positive}` / `{negative}` 占位符路径（作者显式声明的契约）、
种子自动定位（seed 语义明确，多采样器统一注入同一 seed 是想要的行为）。

### 新增 / 修改

- **提示词落点红线升级为报错**：填了提示词但云端没有占位符、映射里也没指定落点
  → 抛 `RHUserError` 中止（原先只是 warning）。报错文案带可复制的映射模板
  `{"nodeId": "<ID>", "fieldName": "text", "fieldValue": "{positive}"}`，
  并提示用「RH 工作流参数探查」查 nodeId。结构拉不到时不误报。
- **极简版新增 `node_overrides` 输入框**：改云端任何参数都要手写。
  该框不支持 `{positive}` / `{negative}`（本节点没有提示词输入框，
  引用了会被替换成空串 = 静默失效），写了会直接报错。
  **随后移除（用户二次拍板）**：极简版的定位就是「一切参数都用云端的，
  本地只负责触发与解码」，多一个用不上的框反而是负担——契约最终定稿为
  **只有 api_key / workflow_id 两个输入框**，要改云端参数请用专业版。
- **README 注明未接入运行时的模块**：`core/snapshot.py` 与 `core/binding.py`
  是为「自动推断」路线打的地基（S2/S3），当前手动映射路线不消费它们，
  目录树里已标注，避免误解为可用功能。
- **803 逐条定位**：`RunningHubError` 带上 `code`；提交处捕获 803 后用已拉到的
  云端结构逐条对照，报「共 N 条，其中 M 条不合法」并点名第几条 / nodeId /
  fieldName / 该节点可用字段，末尾给修法指引。
- **不变量 A 落地到校验**：`validate_injections` 现在也拒绝向连线字段（`[]` 形态）
  注入，报错说明「该字段已被上游连线接管，平台不建议修改」。
  新增 `diagnose_injections` 做逐条（与入参一一对应）诊断。
- **探查节点输出可直接复制的映射片段**：`wf_mod.mapping_templates()`
  为每个字面量字段输出完整 JSON 片段，连线字段标注 × 且不给模板。
- `prompt_link_diagnosis` 语义改为「指引手写映射」（原先是解释自动定位为什么失败）。

### 行为变化（S0 基线已重录）

9 个 fixture 中 c5 / c6a / c7 从「有注入」变为 0 命中 0 注入；`truth` 未改。

### 已回退：通用参数注入重构 S2（发现层 + Snapshot 模型）

新增 `core/snapshot.py`：把 `getJsonApiFormat` 的原始结构固化成带标注的快照——
每个字段标注 `value_kind`（字面量 / 连线 / 未知，用于守住不变量 A）、
每个节点标注执行态（active / bypassed / muted，用于守住不变量 B）、
整张图抽成连线边列表，并提供两个指纹（全量 / 纯拓扑）与 diff，
从而让「云端工作流被改后绑定静默失效」可被检出。

`core/workflow.py:normalize()` 原先会丢弃平台返回的 `_meta`（含用户自定义节点标题
`_meta.title`），现已连同节点直属执行态字段一起保留在 `meta` 里；
`class_type` / `inputs` 两个键不变，既有调用方与自测无需改动。
「RH 工作流参数探查」的清单里现在会显示节点标题。

客户端新增 `get_workflow_snapshot()`（带进程内缓存）。
自测新增 `tests/test_snapshot.py`（101 项，不依赖 torch / API Key），含一节交叉一致性验证：
同一份数据分别走既有 `normalize()` 与新快照，逐节点比对 class_type / 字段集合 / 取值 / meta，
覆盖 `examples/*.json` 与 S0 的 `tests/fixtures/workflows/*.json`。

> **后续变更（2026-10）**：该 Snapshot 模块与 `tests/test_snapshot.py` 已随
> 「自动推断落点」路线一并移除（无消费方，维护成本大于收益）。
> `normalize()` 保留 `_meta` 与节点标题这一项收益仍然有效。
> 回退记录见 `docs/injection-architecture.md` 第六节。

## [1.3.1] - 2026-09-30

### 文档：确认 RunningHub 不允许安装自定义节点，H3 路线改为「云端出片 + 独立音轨」

平台事实（用户确认）：RunningHub 云端不接受用户自行上传安装自定义节点，
但预装了大量主流节点。这条约束让 v1.3.0 的「路线 A（复合 latent 带回本地）」
在 RunningHub 上不可行——要修 `SaveLatent` 对 `NestedTensor` 报错，就必须换节点。

已核实核心里也没有替代方案（`nodes_minimax_h3.py` 与 latent 相关的节点
只有 `EmptyMiniMaxH3LatentAV` 一个，且无条件同时造视频+音频两个张量；
`NestedTensor.__getitem__/chunk` 返回的仍是 NestedTensor，切不散）。

因此 H3 在 RunningHub 上的正确用法改为：

    H3 采样器 →┬─ VAEDecode(H3 视频 VAE)      → VHS_VideoCombine(24fps) → mp4
               └─ VAEDecodeAudio(H3 音频 VAE) → Save Audio (Advanced)   → flac/mp3

音频必须显式接一路 `VAEDecodeAudio`（`nodes_audio.py:100` 取 `unbind()[-1]`），
否则 `VAEDecode` 只取 `unbind()[0]` 会把音频静默丢掉。帧率 24、音频 32kHz。

改动：

- `docs/h3-av-latent.md` 重写：路线 C 升为默认；路线 A/B 标注仅适用于
  允许装自定义节点的环境（本地 / 自建云端 / 其它平台）；新增 2 分钟云端自查
  （万一平台自带能存 AV latent 的节点，路线 A 可复活）
- README：节点表加平台限制说明；视频章节与 FAQ 的 H3 条目改为云端出片方案
- `extras/av_latent_nodes.py`：文件头注明 RunningHub 装不了，改用路线 C

代码逻辑未变，自测仍为 146 项。

## [1.3.0] - 2026-09-29

### 新增：音视频复合 latent 支持（MiniMax H3）

- **原生「保存Latent」存不了 H3 的 latent**：H3 的 latent 不是单个张量，而是
  `NestedTensor((视频 [B,24,T,H/16,W/16], 音频 [B,32,2,T40]))`；原生
  `SaveLatent` 里 `samples["samples"].contiguous()`（`nodes.py:552`）在
  NestedTensor 上必然抛 `'NestedTensor' object has no attribute 'contiguous'`。
  这不是接线错误，是原生节点没覆盖这种结构。
- 新增 3 个节点：
  - **RH AV Latent 保存**：替代原生 SaveLatent，把复合 latent 按
    `latent_0`（视频）/ `latent_1`（音频）写进一个 safetensors 容器。
    扩展名沿用 `.latent`，云端平台才会把它当作 latent 输出收录。
  - **RH AV Latent 拆分**：拆成视频 / 音频两条普通 latent。原生 `VAEDecode`
    遇到复合 latent 只取 `unbind()[0]`、音频被静默丢弃，所以这一步不能省。
  - **RH AV Latent 合并**：把两条 latent 组装回 NestedTensor，供需要完整
    复合 latent 的下游（如二采 / latent 放大）使用。
- `core/latent.py` 新增复合 latent 解析：识别 `latent_0/latent_1`、
  `h3_video/h3_audio` 等键名（兼容社区 `.h3latent` 包），并**固定不做
  0.18215 缩放补偿**——H3 的 scale_factor 就是 1.0，误乘 5.49 倍画面会毁掉。
- 输出分类新增 `.h3latent` / `.avlatent` 扩展名识别。
- report 的形状回显区分音视频，显示为「视频 [...] + 音频 [...]」。
- 新增 `extras/av_latent_nodes.py`：**云端用**的零依赖单文件节点包
  （保存 + 拆分），可单独作为一个 custom_nodes 包安装到云端。
- 新增示例 `examples/05_minimax_h3_av.json`（H3 本地解码链路，含音频合成）。
- 新增文档 `docs/h3-av-latent.md`：失败原因逐行分析、三条路线对比、
  云端/本地操作步骤、音频单独导出剪辑的三种做法、排障清单。

### 测试

- 自测 130 -> **146** 项，覆盖复合 latent 存取往返、不缩放、普通 latent
  不被误判、新扩展名分类、拆分/合并节点与可读报错。

## [1.2.2] - 2026-09-29

### 修复

- **文本处理节点接在提示词前面时静默失效**：ComfyUI 里 widget 一旦被上游连线
  接管，它在工作流 JSON 里就从字符串变成连线引用，往该节点注入 text 会被上游
  覆盖且无任何报错。现在会**沿连线向上游回溯**到真正可写的字符串字段再注入
  （支持多跳，兼容 text / value / string 等不同字段命名）。
  - 上游只有唯一字符串字段 -> 直接注入该字段；
  - 上游字段里有占位符 -> 认这个字段；
  - 上游有多个文本字段且都无占位符 -> **不猜**，保持 0 命中并如实报警。
- 新增 `prompt_link_diagnosis`：定位失败时给出具体到 nodeId 的原因
  （连线源缺失 / 上游多字段 / text 被接管 / 无采样器连线），不再泛泛提示。
- 新增 `placeholder_hits`：本地提示词为空但云端写了占位符时报警——
  否则云端提示词会被替换成空内容，画面对不上却一声不响。
- 专业版 report 现在会回显**实际送进云端的提示词**（前 60 字）。
  输入框一旦连上节点，框里手写的文字就不再生效，这一行是排查该类问题
  最快的一眼定位点。

### 测试

- 自测 117 -> 130 项，覆盖上游回溯、多跳链路、歧义放弃、空内容报警与预览。

## [1.2.1] - 2026-09-29

### 修复

- **提示词在部分工作流上永远不生效**：`getJsonApiFormat` 对某些工作流只返回
  注册为 API 参数的字段清单，CLIPTextEncode 的 text 值不在其中，导致占位符
  扫描恒为 0 命中（云端明明写了 `{positive}` 也报「没找到」）。
  新增**连线跟随兜底**：占位符没命中但用户填了提示词时，沿采样器的
  positive / negative 连线找到编码节点，直接覆盖其 text 字段。
  - 同一字段不会被正、负两个 token 重复注入（positive 优先认领）；
  - 连线源不在返回结构里时安全放弃并保持 0 命中（警告如实上报）；
  - 占位符主路径命中时兜底不触发。
- 「提示词未生效」警告重写：不再说「云端没写占位符」（可能冤枉用户），
  改为报告实际发生的事实（结构读到几个节点、两条路径都没命中），
  并给出在参数映射里直接指定文本节点 ID 的最可靠修法。

### 测试

- 自测 107 -> 117 项，新增覆盖兜底注入的命中、去重、安全放弃与主路径优先。

## [1.2.0] - 2026-09-29

按用户实测反馈做的易用性重构。主线是「极简版做到真·零配置」和「专业版开箱即用」。

### 变更

- **极简版只剩 2 个输入框**（api_key、workflow_id）：
  - 移除了 positive / negative / seed / timeout / poll_interval / use_cache /
    use_high_performance / on_missing_placeholder 全部 widget；
  - 提示词完全沿用云端默认文案；种子每次执行自动随机（`IS_CHANGED` 强制重算）；
  - timeout / poll_interval / use_cache / use_high_performance 改为从 config.ini 读取；
  - 移除 `on_missing_placeholder` 选项，行为统一为「警告继续 + 节点变黄」。
- **专业版开箱即用**：接入与极简版同一套自动注入（占位符全局替换 + 种子），
  提示词不再需要手写映射；`node_overrides` 与自动注入冲突时以映射为准。
- **专业版输入框文案**：提示词框可见文本只显示 `positive` / `negative`；
  参数映射框默认留空，格式说明全部移入悬停提示（tooltip），不再用示例 JSON 当默认值。
- 示例工作流同步重做：极简版示例只有 2 个 widget 值；专业版示例默认映射留空；
  二采示例的映射只保留 denoise 一项（这才是映射的典型用途）。

### 兼容性说明

- 极简版输入结构变了：旧版画布里已摆好的极简版节点需要重新添加。
- 专业版注册名、输入键名与 widget 顺序均未变，已保存的工作流不受影响。

## [1.1.0] - 2026-09-28

按改造需求文档完成的一轮打磨。主线是「消灭静默失效」和「降低上手门槛」。

### 新增

- **极简版节点** `RunningHubLatentGeneratorSimple`：只填 workflow_id 即可出图。
  自动拉云端工作流结构、扫描所有字符串字段替换 `{positive}` / `{negative}`、
  自动定位采样器 seed 字段注入种子
- **提示词全局替换**（`core/workflow.py`）：不依赖 `class_type`，只认占位符文本，
  因此兼容任意结构的工作流；支持前后缀保留（`masterpiece, {positive}, best quality`）
  与大小写变体（`{Positive}`）
- **替换结果校验**：替换命中数为 0 时给出醒目警告，杜绝静默失效
- **参数映射校验**：`validate_mapping` 开启后，nodeId 不存在 / fieldName 无效
  都会在 report 里明确提示
- **API Key 全局配置**：新增 `config.ini`（`config.json` 继续兼容）
- **前端扩展** `web/rh_warning.js`：出现警告时节点边框变黄并弹 toast，
  同时拉高提示词输入框。纯 Python 无法给节点上色，这一步必须靠前端
- **参数映射校验与自动定位结果**可在「RH 工作流参数探查」中直接查看，
  它会告诉你这条工作流能不能被极简版自动注入

### 修复

- **输出类型识别失败**：真实接口返回的字段名是 `outputType`（值为 `Latent`），
  此前只读 `fileType`。现同时兼容 `fileType` / `outputType` / `type`，并统一小写比较
  （原实现其实已经做了 lowercase，真正的原因是字段名不同，不是大小写）
- **任务成功但无输出**：对象存储文件落地比状态更新慢，首次拿到空列表时
  等待 2 秒补查一次，仍为空才报错。此前会白白损失一次已付费的任务
- **响应形态兼容**：`data` 可能是 list、`{"results": [...]}`、`{"outputs": [...]}`、
  单个文件 dict，或带 `status` 字段，现已全部兼容
- **中文文件名**：下载时原样把 URL 交给 requests，不做任何手工编解码
- **排错信息增强**：识别不出 latent 时会附上云端原始返回（脱敏后）一起展示

### 变更

- 主节点显示名改为 **RH 云端采样 · 专业版**。
  注册名仍为 `RunningHubLatentGenerator` —— 改名会让用户已保存的工作流全部断线
- 三个文本框补齐 tooltip 与 placeholder，说明占位符规则
- 示例工作流改为 4 份：极简 / 专业单采样 / 视频 / 二采高清修复，均内置中文注释
- 自测从 49 项扩充到 **104 项**，新增覆盖输出类型大小写、空结果补查、
  工作流归一化、提示词替换、种子定位、映射校验、config.ini 解析

## [1.0.0] - 2026-09-27

首个可用版本。

### 新增

- **4 个节点**
  - `RunningHubLatentGenerator` 主节点：提交 → 轮询 → 下载 → 输出标准 LATENT
  - `RunningHubLatentFetch`：按 taskId 重新取回，用于超时续取与跨会话复用
  - `RunningHubLatentFromUrl`：直接加载任意 latent 下载链接
  - `RunningHubWorkflowInspector`：拉取云端工作流的可修改参数清单
- **通用的参数注入机制**：`node_overrides` 支持任意 nodeId / fieldName，
  并用 `{positive}` / `{negative}` / `{seed}` 占位符绑定到节点 widget，
  从而在不牺牲通用性的前提下保留「拖滑块调 seed」的原生体验
- **本地 latent 缓存**：按「工作流 ID + 参数」哈希，同参数重复运行不消耗云端额度
- **配置文件支持**：`config.json` 保存 API Key 与默认工作流，已加入 `.gitignore`
- **图像 / 视频双支持**：4 维图像 latent 与 5 维视频 latent 均可解析输出
- **全流程中文报错**：无效 Key、网络中断、额度不足、并发超限、云端节点级失败
  均有明确中文提示，且异常不会导致 ComfyUI 崩溃

### 实现要点

- latent 解析严格对齐 ComfyUI 原生 `LoadLatent`：带 `latent_format_version_0`
  标记的文件不做缩放补偿，老文件才乘 `1/0.18215`。该版本标记是空**张量**而非
  safetensors metadata（`load_file` 不会返回 metadata），这点已在 ComfyUI 源码中确认
- 自动纠正 `value` → `fieldValue`，避开 RunningHub API 最常见的调用错误
- 多处输出 latent 时沿 batch 维合并；形状不一致时取第一个并给出警告
- 所有异常消息对 API Key 做打码处理，杜绝密钥泄漏到 ComfyUI 前端

### 说明

立项文档建议 fork `ComfyUI_NetDist` 改造，最终未采用，原因见 README「设计说明」：
两者通信协议不匹配、可复用部分仅约 20 行，且 NetDist 的 Apache-2.0 与 MIT 发布存在冲突。
