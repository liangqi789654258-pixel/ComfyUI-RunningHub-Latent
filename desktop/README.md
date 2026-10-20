# RH 出图器（RH-Standalone）

不打开 ComfyUI，直接把 RunningHub 云端当远程采样器出图/出片：
**填参数 → 云端采样 → 下载 latent → 本地 VAE 解码 → PNG / mp4 落盘进画廊**。

## 启动

**桌面双击「RH出图器」图标即可**（首次运行任意入口后会自动创建桌面快捷方式，
带应用图标、无黑窗口）。也可双击应用目录里的 `start_rh_standalone.bat`
（使用 ComfyUI 自带 venv 的 Python，无需另装环境）。

## 界面能帮你的五件事

| 功能 | 在哪 | 解决什么 |
|---|---|---|
| **启动自检** | 启动时自动 / 工具栏「⋯」→ 环境自检 | ComfyUI 路径、插件目录、VAE、ffmpeg、Key 一次查完，缺哪项给修复入口 |
| **任务台账** | 工具栏「⋯」→ 任务台账 | 时间 / 任务号 / 结果 / 积分 / 耗时 / 失败原因，一眼对账 |
| **每日积分上限** | 设置 → 每日积分上限 | 超过上限时生成前先确认，防手滑连点 |
| **按 taskId 续取** | 工具栏「⋯」→ 按 taskId 续取 | 超时/断网/误关窗口也能把已付费的结果捞回来，不重复扣费 |
| **托盘通知** | 自动 | 出视频要等几分钟，最小化后完成/失败弹系统通知 |

## 首次使用

0. **从哪开始**：如果启动自检弹了红叉，先按它提示修——那是最省时间的入口。
1. **API Key**：应用内「设置」→ 粘贴 RunningHub API Key → 保存
   （在 ComfyUI 里已经配过的话，点「从插件 config.ini 导入」一键抄过来）。
   只存在本机 `data/app_config.json`，一次填写持续生效；**不会**嵌进生成的
   图片/视频元数据，也不上传。未设置 Key 时生成会明确报错（无任何隐式取 Key 路径）。
   > ⚠️ 发布/打包注意：`data/`（含 Key）与插件 `config.ini` 绝不能进 git
   > 仓库或 Release 压缩包。
2. **生成预设**：点「从工作流 ID 生成预设」，输入云端工作流 ID。
   应用自动拉取参数结构，生成映射骨架（`{seed}` / `{positive}` / `{negative}` 占位符）
   与全部可注入字段的参考清单。
3. **填参数 → 生成**：映射里用 `{positive}` `{negative}` `{seed}` 占位符，
   运行时替换为界面填写的真实值（整值占位符保留类型）。
4. **VAE**：默认扫描 `ComfyUI/models/vae`，可在设置里改目录。解码时 ComfyUI
   主程序不运行，显存独占；显存不足自动降级分块解码。

## 视频（出片）

云端工作流末端接「保存Latent」后，只要 latent 是 5 维视频 latent（时间维 > 1），
应用会自动走视频路：**分块解码 → H.264 mp4**（`output/日期/RH_时间_任务号_video.mp4`）。

## 复用出图参数 & 拖入 ComfyUI 还原工作流

参数直接**嵌进媒体文件自身**（零多余文件），一份元数据两处消费：

- **拖进 ComfyUI**：PNG / mp4 都能直接还原出可执行的工作流——
  「RH 云端采样·专业版（当时的 workflow_id / 提示词 / 种子 / 参数映射）→
  VAE加载器 → VAE解码 → 保存图像」，点运行即可等价重跑（API Key 不嵌入，需自行填写）。
  视频还原依赖 VHS 插件（ComfyUI 拖 mp4 的标准机制）。
- **画廊右键 → 复用参数**：一键回填当时的全部参数（种子固定为原值方便复现；
  想抽卡勾回「每次随机」）。菜单里还有「打开文件」「打开所在文件夹」。

- **分块档位**（主界面「视频解码」可选，默认自动按分辨率/帧数挑）：

  | 档位 | tile | overlap | temporal | temporal_overlap |
  |---|---|---|---|---|
  | 480p（832×480） | 384 | 64 | 32 | 8 |
  | 720p（1280×720） | 320 | 64 | 16 | 4 |
  | 720p 长片段（>121 帧） | 256 | 64 | 16 | 4 |

  显存不够时自动沿「降时间维 → 缩分块」阶梯降档重试（状态栏会提示）。
- **帧率**默认 16（Wan 2.1 原生）；H3 是 24fps——帧率错了会快放/慢放。
- **VAE 别装错**：视频 latent 必须用对应视频 VAE（Wan 系列用 `wan_2.1_vae`，
  别用 `wan2.2`）。用图片 VAE 会静默只出一张图、不报错——应用对此已有通道守卫，
  但选对 VAE 仍是第一原则。
- **云端超时**：视频采样 10 分钟起步，先在设置里把超时调到 1200~2400 秒。
- mp4 编码用 ffmpeg（优先取 imageio-ffmpeg 自带，ComfyUI venv 已装；
  缺失时在 ComfyUI 的 Python 环境执行 `python -m pip install imageio-ffmpeg`）。

H3 音视频复合 latent（NestedTensor）暂未接入，会明确报错并把文件留在缓存里。

## 设置项（应用内「设置」对话框）

- 输出目录（默认 `output/`，按日期分组存放）
- VAE 目录 / ComfyUI 根目录
- 界面语言（简体中文 / English，切换后重启生效）
- 云端超时 / 轮询间隔 / 超时续等轮数

## 架构（一句话）

GUI（PySide6）→ 引擎（本应用 `app/engine/`）→ **复用插件 core**（client/latent/
workflow/errors/cache/ledger/settings，单一代码源，不复制）→ **只读 import ComfyUI
的 `comfy.sd`** 做 VAE 解码（不修改 ComfyUI 本体）。

## 发布给别人用（GitHub 发布形态）

**目标用户 = ComfyUI 用户**——他们机器上已有 Python + torch（重依赖），
所以不做几 GB 的独立 exe，而是**源码发布 + 一键安装脚本复用 ComfyUI 的 Python**。

推荐把本应用作为插件仓库的 `desktop/` 子目录一起发布（一份仓库、一份 core）：

```
ComfyUI-RunningHub-Latent/        ← 插件仓库（custom_nodes 里装的就是它）
├── core/  nodes/  web/  ...
└── desktop/                      ← 本应用整个目录
    ├── install.bat               ← 一键安装（自动检测 ComfyUI python）
    └── app/  main.py  ...
```

**新用户三步上手：**

1. 装 ComfyUI 插件（Manager 搜 ComfyUI-RunningHub-Latent，或 git clone 进
   `custom_nodes/`）；
2. 进 `custom_nodes/ComfyUI-RunningHub-Latent/desktop/`，双击 `install.bat`：
   自动检测 ComfyUI 的 python（`.venv` / 便携版 `python_embeded` 都认，
   也可手动传路径），只补装 PySide6 / pywin32 两个轻依赖（约 100MB，一次性）；
3. 启动后在「设置」里填 **API Key**（存在本机 `data/app_config.json`，不上传），
   「从工作流 ID 生成预设」→ 填提示词 → 生成。

应用查找 core 的顺序：`上一级目录/core`（装在插件内时）→
`ComfyUI/custom_nodes/ComfyUI-RunningHub-Latent`（独立摆放时），两种布局都兼容。

## 版本范围

- P0：图像 latent → PNG；预设自动生成；映射编辑器（模板 + 预校验）；
  作品画廊；中英双语。
- P1（当前）：**视频 latent → mp4（分块档位 + OOM 自动降档 + ffmpeg 编码）**；
  待做：H3 音视频复合 latent → mp4+音轨；托盘通知；台账查看器。
- 视频与音视频 latent 已会**缓存到本地**，升级后无需重跑云端任务。

## 测试

```bat
C:\AI\dongshan\ComfyUI\.venv\Scripts\python.exe tests_smoke.py
C:\AI\dongshan\ComfyUI\.venv\Scripts\python.exe tests_video.py
C:\AI\dongshan\ComfyUI\.venv\Scripts\python.exe tests_gui_offscreen.py
```

## 合规

遵守 RunningHub 平台规则与当地法律法规使用。本应用与
ComfyUI-RunningHub-Latent 插件共用许可与免责声明（见插件目录 LICENSE / README）。
