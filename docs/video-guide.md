# 视频出片方案：云端采样 + 本地解码

> 适用插件版本：v1.2.2 ｜ 目标机型：RTX 3060 Laptop 6.0 GB ｜ 日期：2026-09-29
> 本文中的所有参数与代码位置，均已在 `C:\AI\dongshan\ComfyUI`（v0.37.0）源码中核实，非推测。

---

## 0. 一句话结论

**能做，而且视频才是这套插件最该干的活。** 你本机**没有任何 Wan 采样模型**（unet / clip 目录是空的），本地根本跑不了视频采样；但你本机**有 Wan 的 VAE**，解码完全撑得住。所以「云端采样 → 传 latent → 本地解码」对视频不是优化，是**唯一可行路径**。

通用套路对所有模型都成立（SDXL 图片、Flux、Wan 视频通用）：

```
任意 ComfyUI 工作流
    → 导入 RunningHub
    → 末端换成「保存Latent」
    → 本地填 API Key + 工作流 ID
    → 本地接 VAE 解码
```

视频只是把最后一步的 VAE 换成**视频 VAE**，并多调两个时间维度参数。

---

## 1. 先看清本机现状（这是方案的立足点）

| 目录 | 内容 | 对视频方案的含义 |
|---|---|---|
| `models/diffusion_models` | Anima、Qwen 2、ZImageTurbo、Unknown | ❌ 无 Wan 系列 |
| `models/unet` | 空 | ❌ 无 Wan 主模型 |
| `models/clip` | 空 | ❌ 无 Wan 文本编码器（umt5） |
| `models/vae` | `wan_2.1_vae.safetensors`（253 MB）<br>`wan2.2_vae.safetensors`（1.4 GB） | ✅ 解码能力具备 |

**结论：采样必须上云，解码可以在本地完成。** 本机连 Wan 采样的一次前向都跑不起来，但 VAE 解码是可分块控制的。

---

## 2. 数据链路

```
【云端 RunningHub】                          【本地 ComfyUI（6GB）】

  文本编码 CLIPTextEncode                      ┌─────────────────────────┐
      ↓                                       │ RH 云端采样·专业版       │
  空视频 latent (length/w/h)                   │  api_key + workflow_id  │
      ↓                                       │  {positive}/{negative}  │
  KSampler  ← 种子注入                    ───▶│  node_overrides         │
      ↓                                  网络 └───────────┬─────────────┘
  ★保存Latent★ ← 关键：末端必须是它        仅几 MB            │ LATENT (5维)
      ↓                                                    ↓
  云端不解码、不出片                              VAELoader(wan_2.1_vae)
                                                          ↓
                                                 VAEDecodeTiled
                                                  tile_size / temporal_size
                                                          ↓ IMAGE（帧批）
                                                 VHS_VideoCombine(16fps)
                                                          ↓
                                                      .mp4
```

**核心：云端只跑到 latent 就停，一次 VAE 解码都不做。** 解码放在本地，好处有三个——显存可控（可分块）、不占云端额度、成片参数（帧率/编码）随时改不用重跑。

---

## 3. 云端工作流怎么搭

### 3.1 两种方式，推荐第一种

| 方式 | 做法 | 适用 |
|---|---|---|
| **A（推荐）** | 在 RunningHub 上从 **Wan 2.1 文生视频模板**新建，然后把末端输出节点删掉，换成「保存Latent」 | 上手快，模型齐全 |
| **B** | 本地搭好 → 导出 JSON → 导入 RunningHub → 改末端 | 已有工作流要复用 |

> ⚠️ 方式 B 有个坑：本机 v0.37.0 **没有** `EmptyWanLatentVideo` 节点（只有 `EmptyHunyuanLatentVideo`）。
> 两者语义可互换——源码里 `EmptyHunyuanLatentVideo.execute()` 生成的是
> `[batch, 16, ((length-1)//4)+1, height//8, width//8]`，与 Wan21 的 16 通道 / 时间 4× / 空间 8× 完全一致。
> 云端 RunningHub 若版本较新，直接用 `EmptyWanLatentVideo` 即可。

### 3.2 云端节点清单（Wan 2.1 T2V）

| # | 节点 | 关键参数 | 说明 |
|---|---|---|---|
| 1 | `UNETLoader` | `wan2.1_t2v_1.3B_fp16`（先小后大，14B 更慢） | 主模型 |
| 2 | `CLIPLoader` | `umt5_xxl_fp8_e4m3fn_scaled`，type=`wan` | 文本编码器 |
| 3 | `CLIPTextEncode` ×2 | 正面框里写 `{positive}`、负面框里写 `{negative}` | **占位符必须原样带花括号** |
| 4 | `EmptyWanLatentVideo` | `width` / `height` / `length` | latent 尺寸与帧数 |
| 5 | `KSampler` | seed 任意（会被本地覆盖）、steps 30、cfg 5~6 | 采样器 |
| 6 | **`SaveLatent`** | `filename_prefix` 随意 | ★末端唯一输出节点★ |

### 3.3 末端必须是 SaveLatent，且最好只留它

- 云端**不要**留 `VAEDecode` + `VHS_VideoCombine`。留着的话云端会先做一次完整解码，既慢又费额度，而本地拿到的还是 latent，等于白跑一遍 VAE。
- 插件有保护：如果检测到云端还输出了非 latent 文件，report 会提示「云端还额外输出了 N 个非 latent 文件，本次已忽略。建议到云端工作流删除这些输出节点」。看到这句话就去云端删。

### 3.4 怎么拿到 nodeId（填 `node_overrides` 用）

专业版的 `node_overrides` 需要 `(nodeId, fieldName, fieldValue)` 三元组。取 nodeId 的方法：

1. RunningHub 画布上选中节点，右键 → 查看节点 ID；
2. 或在 ComfyUI 设置里开启「显示节点 ID」（`Settings → Comfy → Node ID Badge`）。

常用的三个注入目标：

```json
[
  {"nodeId": "<EmptyWanLatentVideo 的 ID>", "fieldName": "length", "fieldValue": 81},
  {"nodeId": "<EmptyWanLatentVideo 的 ID>", "fieldName": "width",  "fieldValue": 1280},
  {"nodeId": "<EmptyWanLatentVideo 的 ID>", "fieldName": "height", "fieldValue": 720}
]
```

> 提示：`node_overrides` 与自动注入（提示词/种子）可以共存，**手动映射优先级更高**。
> 所以你只需要写 length/width/height 这三条，提示词和种子照样走占位符自动注入。

### 3.5 极简版能不能跑视频？

**能，但有前提**：极简版没有 `node_overrides` 输入框，改不了帧数和分辨率。所以要把 `length` / `width` / `height` **在云端工作流里写死**。

- 想固定规格批量出片（比如永远 720p/81 帧）→ 极简版完全够用，只靠 `{positive}`/`{negative}` + 自动随机种子；
- 需要随时改帧数/分辨率 → 用专业版。

---

## 4. 本地工作流怎么搭

打开 `examples/03_video_latent.json` 即可，链路是四个节点：

```
RH 云端采样·专业版
    ↓ LATENT
VAELoader  →  wan_2.1_vae.safetensors
    ↓ VAE
VAEDecodeTiled  →  tile_size / overlap / temporal_size / temporal_overlap
    ↓ IMAGE
VHS_VideoCombine  →  frame_rate 16 / h264-mp4
```

### 4.1 VAE 选型：用 2.1，别用 2.2

| 文件 | 通道 | 空间压缩 | 体积 | 6GB 建议 |
|---|---|---|---|---|
| `wan_2.1_vae.safetensors` | 16 | 8× | 253 MB | ✅ **用它** |
| `wan2.2_vae.safetensors` | 48 | **16×** | 1.4 GB | ❌ 光加载就吃掉大半个显存 |

两份 VAE 的通道数和压缩比都不同，**不能混用**——哪个模型采的样，就用哪个 VAE 解。

### 4.2 解码参数（这是唯一需要调的地方）

`VAEDecodeTiled` 后两个参数藏在 **advanced** 区里，要展开才看得到。

| 目标 | tile_size | overlap | temporal_size | temporal_overlap |
|---|---|---|---|---|
| 480p（832×480） | 384 | 64 | 32 | 8 |
| **720p（1280×720）** | **320** | **64** | **16** | **4** |
| 720p 长片段（>121 帧） | 256 | 64 | 16 | 4 |
| 1080p | — | — | — | — |

参数含义与两个易错点：

1. **`temporal_size` 的单位是「像素帧」，不是 latent 帧。** 源码里会先除掉时间压缩比：
   ```python
   temporal_compression = vae.temporal_compression_decode()   # Wan = 4
   temporal_size = max(2, temporal_size // temporal_compression)
   ```
   所以填 16 → 一次只解 4 个 latent 帧 → 输出 16 帧像素。**填 64（默认值）等于一次解 64 帧，6GB 必炸。**
2. **装不下就先降 `temporal_size`，比调 `tile_size` 见效快得多。** 帧维度的中间张量是显存大头。
   `temporal_overlap` 给 4~8 即可，用来抹掉分段边界，肉眼看不出接缝。

1080p 不建议在 6GB 上做——不是 latent 装不下，是解码中间态撑不住。

---

## 5. latent 大小与耗时参考（Wan 2.1）

16 通道、时间 4×、空间 8×；latent 帧数 = `(帧数-1)//4 + 1`。

| 分辨率 | 帧数 | latent 形状 `[1,C,F,H,W]` | latent 文件 | 建议 timeout |
|---|---|---|---|---|
| 832×480 | 33 | `[1,16,9,60,104]` | ~1.7 MB | 1200 s |
| 832×480 | 81 | `[1,16,21,60,104]` | ~4.0 MB | 1800 s |
| 1280×720 | 33 | `[1,16,9,90,160]` | ~4.0 MB | 1800 s |
| 1280×720 | 81 | `[1,16,21,90,160]` | ~9.2 MB | **2400 s** |
| 1280×720 | 121 | `[1,16,31,90,160]` | ~13.6 MB | 3000 s |
| 1280×720 | 161 | `[1,16,41,90,160]` | ~18.0 MB | 3600 s |

**latent 传输完全不是瓶颈**（最大的也就 18 MB），瓶颈在两头：云端采样时长、本地解码显存。

> 注意：插件解析时会统一转成 float32（`core/latent.py`），所以内存占用约为上表的 2 倍，仍然很小。

**必须改的一个配置**：`config.ini` 里 `timeout` 默认 600 秒，对视频远远不够。

```ini
[runninghub]
timeout = 2400        ; 720p/81帧 建议值，专业版节点上也可直接调到 2400（上限 3600）
poll_interval = 10    ; 视频任务不用问那么勤，5 秒改成 10 秒即可
```

---

## 6. 三个必踩的坑

### 坑 1：装错 VAE 会「静默只出一张图」

`comfy/sd.py` 里有这么一段：图片 VAE 收到 5 维输入时，执行 `samples_in[:, :, 0]`——**只取第一帧，而且不报错**。

表现：一切正常，无报错，但输出只有一张图。排查方式是看 VAELoader 选的是不是 `wan_*`。

### 坑 2：`temporal_size` 用默认值 64 必炸显存

见 4.2。这是 6GB 上最常见的一次性失败点。

### 坑 3：timeout 600 秒会中途放弃

视频采样 10 分钟起步。任务在云端其实还在跑，但本地已经超时报错了——**钱花了，图没拿到**。一定先调 timeout。

---

## 7. 首次实操清单（按这个顺序，别跳步）

| 步骤 | 动作 | 通过标准 |
|---|---|---|
| 1 | 云端建 Wan2.1 T2V 工作流，末端 `SaveLatent`，**480p / 33 帧** | 云端手动跑一次能成功 |
| 2 | 云端正面框填 `{positive}`、负面框填 `{negative}`，保存 | — |
| 3 | `config.ini` 里 `timeout = 1200` | — |
| 4 | 本地打开 `examples/03_video_latent.json`，填 api_key / workflow_id | — |
| 5 | 跑一次，看 report **第一行**回显的提示词 | 显示的是你写的提示词 |
| 6 | 看 report 里的「张量形状」 | **必须是 5 维** `[1,16,9,60,104]` |
| 7 | 若第 6 步是 4 维 | 云端末端没接对 SaveLatent，回去查步骤 1 |
| 8 | 解码成功后再把帧数/分辨率往上加 | 每次只加一项 |

**第 6 步是整个方案的生死线**：拿到 5 维就说明链路通了，剩下的都是调参。
如果 RunningHub 的「保存Latent」节点不支持 5 维 latent（目前唯一未经真机验证的环节），
report 会报「latent 张量维度异常」，届时把 report 全文发出来即可定位。

---

## 8. 本地成片

`VHS_VideoCombine` 参数：

| 参数 | 值 | 说明 |
|---|---|---|
| `frame_rate` | **16** | Wan 2.1 原生帧率；默认 8 会变慢动作 |
| `filename_prefix` | `RH_Video` | 输出在 `output/video/` |
| `format` | `video/h264-mp4` | 已装 `imageio-ffmpeg`，重启 ComfyUI 后该选项会出现 |
| `loop_count` | 0 | — |
| `pingpong` | false | — |

> ffmpeg 已于 2026-09-29 装入 `C:\AI\dongshan\ComfyUI\.venv`（`imageio-ffmpeg 0.6.0`）。
> **需重启 ComfyUI 才会生效**；重启前 format 下拉里只有 gif / webp。

**备选路径**：`VHS_VideoCombine` 的 optional 输入里也有 `vae`，可以直接把 LATENT 接到它的 `latents` 口、VAE 接到 `vae` 口，由 VHS 内部解码。好处是省一个节点，坏处是**解码分块参数不可控**，6GB 上不如 `VAEDecodeTiled` 稳。推荐保持显式解码。

---

## 9. 故障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| report 显示 4 维形状 | 云端末端不是 SaveLatent / 传的是图 latent | 查云端末端节点 |
| 不报错但只出 1 张图 | VAELoader 选了图片 VAE | 换成 `wan_2.1_vae` |
| 解码时 OOM | `temporal_size` 太大 | 先降到 16，再降到 8 |
| 任务超时但云端在跑 | `timeout` 太小 | 调到 2400+ |
| 画面闪烁 / 帧间跳变 | 视频 latent 被错误缩放 | 检查是否走了老格式分支（正常不会，见 10） |
| 提示词没生效 | 上游连线接错输出口 | 看 report 第一行回显；详见 README「常见问题排查」 |
| report 提示有额外非 latent 文件 | 云端留了 SaveImage / VHS 输出 | 去云端删掉 |

---

## 10. latent 缩放：视频为什么不用改任何东西

`core/latent.py` 的规则是：容器里**有** `latent_format_version_0` 标记 → 不缩放；**没有** → 乘 `1/0.18215`。

这与 ComfyUI 原生行为严格一致（`nodes.py:554` SaveLatent 写入标记，`nodes.py:578` LoadLatent 无标记才补偿）。
云端 `SaveLatent` 产出的文件带标记，所以 5 维视频 latent 会被**原样**交给本地 VAE，
语义等同于本地跑完整流程——**这条规则对图片和视频是一样的，视频不需要任何特殊处理。**

反过来说，一旦误乘 5.49 倍，画面会直接崩成噪声。所以这里刻意不做任何「贴心推断」。

---

## 11. 待真机验证的一项

**RunningHub 的「保存Latent」节点是否支持 5 维视频 latent。** 原理上没有障碍（safetensors 支持任意维度，
`SaveLatent` 也只是把张量原样写进容器），但这一环尚未用真实视频任务验证过。

按第 7 步用 **480p / 33 帧**做最小验证即可，成本最低（几毛钱 + 几分钟）。通过了再往上加规格。
