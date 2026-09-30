# MiniMax H3：云端采样 + 本地解码

> 适用插件版本：v1.3.1+
> 本机核实环境：ComfyUI 0.37.0 / torch 2.13.0 / RTX 3060 6GB
> 文中所有行号与行为均在你本机 ComfyUI 源码上实测确认，非推测。
> **2026-09-30 更新：已确认 RunningHub 云端不允许用户安装自定义节点 → 路线调整为「云端出片 + 独立音轨」。**

---

## 0. 结论先行（已按平台约束更新）

**平台事实（用户 2026-09-30 确认）**：RunningHub 云端**不接受用户自行上传安装自定义节点**，但工作台预装了大量主流节点。

这个约束带来一个直接后果：**H3 的 latent 在 RunningHub 上带不回来。**

原因是两件事叠加：

1. H3 的 latent 是「视频 + 音频」打包的 `NestedTensor`，原生「保存Latent」在上面必然报错（第 1 节）；
2. 修复这个报错需要换一个能处理 NestedTensor 的保存节点——**而这需要装自定义节点，云端不允许**。

我用核心源码逐条核过，**预装节点里也没有任何替代方案**：

- `comfy_extras/nodes_minimax_h3.py` 里跟 latent 有关的节点**只有 `EmptyMiniMaxH3LatentAV` 一个**，而且它没有"只出视频"的选项——`_empty_av_latent()` 无条件同时造 video 和 audio 两个张量；
- 全仓搜不到任何把复合 latent 拆成两个普通张量的核心节点；
- `NestedTensor.__getitem__` / `chunk` 返回的**仍是 NestedTensor**（`comfy/nested_tensor.py`，`apply_operation` 逐元素运算后重新打包），所以即便拿 `LatentFromBatch` 之类的节点"切一刀"，送到 SaveLatent 手里还是复合对象，照样炸。

**所以 H3 的正确用法是：云端整体出片，音频单独导出成独立音轨。** 见第 3 节路线 C。

好消息：**你要的「音频先保存、后面自己剪辑」完全不受影响**，而且不依赖任何新节点——云端 `VAEDecodeAudio` + 保存音频节点就能导出独立音轨（第 5 节）。

**唯一值得花 2 分钟试一把的例外**：万一 RunningHub 的 H3 适配里自带了能存 latent 的节点（比如平台的「保存Latent」是他们自己改过的版本，或者预装了社区 H3 latent 包）。自查方法见第 8 节——成了就能走路线 A，成本只是跑一次最短时长。

---

## 1. 为什么存不了：精确到行

> 这一节即使走路线 C 也值得看——它同时解释了「为什么视频 latent 拿不到」和「为什么直接接 VAEDecode 会静默丢音频」。

### 1.1 H3 的 latent 不是一个张量

`comfy_extras/nodes_minimax_h3.py:84`：

```python
def _empty_av_latent(width, height, length, batch_size=1):
    video = torch.zeros([batch_size, 24, latent_t, height // 16, width // 16], ...)
    audio = torch.zeros([batch_size, 32, 2, audio_t], ...)
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count
```

| 成员 | 形状 | 含义 |
|---|---|---|
| video | `[B, 24, T, H/16, W/16]` | 视频 latent，24 通道，空间压缩 16× |
| audio | `[B, 32, 2, T40]` | 音频 latent，32 通道，立体声 |

### 1.2 NestedTensor 是纯 Python 容器

`comfy/nested_tensor.py` 全文只有 `tensors` / `is_nested` / `unbind()` / `__add__` / `__getitem__` 等，**没有任何张量方法**（没有 `contiguous`、没有 `cpu()` 之外的存储接口）。

### 1.3 原生 SaveLatent 偏偏直接调用了张量方法

`nodes.py:552`：

```python
output["latent_tensor"] = samples["samples"].contiguous()
```

实测报错：

```
AttributeError: 'NestedTensor' object has no attribute 'contiguous'
```

### 1.4 佐证：别的节点都处理了，唯独 SaveLatent 没有

- `VAEDecode`（`nodes.py:334`）：`if latent.is_nested: latent = latent.unbind()[0]` → 取**视频**
- `VAEDecodeAudio`（`nodes_audio.py:100`）：`if latent.is_nested: latent = latent.unbind()[-1]` → 取**音频**

两个都做了兼容，唯独 SaveLatent 没做。这是 ComfyUI 的疏漏，不是接线问题。

---

## 2. 第二个坑（比第一个更容易踩）

就算拿到了完整 latent，直接接 `VAEDecode` 会**静默丢掉音频**——它只取 `unbind()[0]`，音频被扔掉且**不报错**。H3 最值钱的立体声就这么没了。

**这也正是路线 C 里必须显式接一路 `VAEDecodeAudio` 的原因**：别指望解码视频的节点顺手把音频给你带出来。

---

## 3. 三条路线（按当前平台约束重新排序）

| 路线 | 前提 | 音频结果 | 当前状态 |
|---|---|---|---|
| **C 云端出片 + 独立音轨** | 无需任何新节点 | 云端单独导出音轨文件 | ✅ **RunningHub 上的默认推荐** |
| **A 复合 latent 带回本地** | 云端能装「AV Latent 保存」 | latent 带回，无损 | ❌ RunningHub 上不可行（云端不让装节点）；自建 / 本地 / 其他平台可用 |
| **B 云端拆开、音频云端解** | 云端能装「AV Latent 拆分」 | 云端出 mp3/flac | ❌ 同上 |

**在 RunningHub 上，H3 就走路线 C。** 这不是妥协：H3 是 33B 模型，耗时大头在采样，VAE 解码占比很小，云端多做的那一步解码换不来多少时间，而本地要为此装 2.8–5.2GB 的视频 VAE，还得承担"存不了 / 丢音频"的风险。

> 顺带一句：我们这套「云端 latent + 本地解码」的甜点场景是 **Wan 2.1 这类普通视频模型**（latent 是干净的单张量、本机已有 `wan_2.1_vae`），对 H3 不成立。别为了统一流程硬套。

---

## 4. 路线 C：详细操作步骤（RunningHub 上照这个改）

### 4.1 云端工作流

在**你现有的 H3 工作流**上，把采样器输出后面接成这样（两个分支并行，全部是核心/预装节点）：

```
H3 采样器 → LATENT(复合)
    ├─ VAEDecode(H3 视频 VAE)      → IMAGE → VHS_VideoCombine(24fps) → mp4
    └─ VAEDecodeAudio(H3 音频 VAE) → AUDIO → Save Audio (Advanced)   → flac/mp3
```

要点：

- **「保存Latent」节点删掉或 bypass**，留着只会报错。
- 视频和音频用**两个不同的 VAE**：视频走 H3 video VAE，音频走 H3 audio VAE，别混。
- `VAEDecode` 自己会取视频分支，**不用**任何拆分节点；但音频必须**显式**再接一路 `VAEDecodeAudio`，否则音频静默丢失（第 2 节）。
- 帧率填 **24**（H3 是 24fps，不是 Wan 的 16）。
- 想一步出带音轨的 mp4，就把 `VAEDecodeAudio` 的输出同时接到 `VHS_VideoCombine` 的 `audio` 输入；**想留独立音轨剪辑就再加一个保存音频节点**，两者不冲突。

### 4.2 保存音频用哪个节点

核心自带的三个，按需要选：

| 节点 | 格式 | 备注 |
|---|---|---|
| **Save Audio (Advanced)** `SaveAudioAdvanced` | flac / mp3 / opus 可选 | **推荐**，剪辑用 flac 无损 |
| Save Audio (MP3) `SaveAudioMP3` | mp3（V0/128k/320k） | 够用，标记 deprecated 但功能正常 |
| Save Audio (FLAC) `SaveAudio` | flac | 同上，deprecated |

H3 音频是 **32kHz 立体声**，导出 flac 能完整保留，后期剪辑再转码不损失。

### 4.3 本地这边做什么

路线 C 下本地基本没活：下载云端产出的 mp4 + 音轨，进剪辑软件（剪映 / PR / DaVinci）合成、对轨、剪。

如果要在本机 ComfyUI 里合轨：

```
VHS_LoadVideo(mp4) → IMAGE ─────────────┐
LoadAudio(音轨) ───→ AUDIO ─────────────┴→ VHS_VideoCombine(24fps) → 成片
```

**别在本地重新解码**——路线 C 拿到的已经是像素，再编码回 latent 再解一遍只会白白损失画质。

---

## 5. 音频单独拿出来剪辑（三种做法）

**做法 1（当前推荐）：云端直接导出音轨**

```
云端：采样器 → VAEDecodeAudio(H3 音频 VAE) → Save Audio (Advanced, flac)
本地：剪辑软件 / LoadAudio → VHS_VideoCombine.audio
```

不依赖任何新节点，路线 C 标配。

**做法 2：云端同时出「带音轨 mp4」+「独立音轨」**

`VAEDecodeAudio` 的输出**同时**接 `VHS_VideoCombine.audio` 和一个保存音频节点。成片和音轨都拿到，成片可直接看，音轨拿去精修。

**做法 3（仅在能装自定义节点的环境）：本地从 latent 解出后导出**

```
本地：RH AV Latent 拆分 → audio → VAEDecodeAudio → SaveAudioAdvanced → wav/flac
```

音频以 latent 形式带回来是无损的，可以同一份 latent 解多次。RunningHub 上这条走不通。

**对齐提醒**：H3 视频 **24fps**、音频 **32kHz 立体声**。本地成片帧率填错就会音画不同步。

---

## 6. 本地解码的硬参数（路线 A 适用，供备查）

| 项目 | 值 | 出处 |
|---|---|---|
| 视频 latent 通道 | 24 | `latent_formats.py:623` |
| 空间压缩 | 16× | `latent_formats.py:625` |
| latent 帧数 | `((F-5)//17)*5 + 2`（F 为像素帧） | `nodes_minimax_h3.py:43` |
| 音频 latent | `[B,32,2,T40]`，T40 = 时长×40 | `nodes_minimax_h3.py:34/50` |
| **scale_factor** | **1.0**（不做缩放补偿） | `latent_formats.py:627` |
| 帧率 | 24 | `nodes_minimax_h3.py:33` |
| 音频采样率 | 32 kHz | `nodes_minimax_h3.py:77` |

**关于 0.18215 缩放规则**：H3 的 `scale_factor` 就是 1.0，插件在解析复合 latent 时**固定不做补偿**（`core/latent.py` 的 `parse_av`）。这条不能改——误乘 5.49 倍画面会直接毁成噪声。

**volume**：124 帧 / 768×1344 的视频 latent 约 14MB，传输从来不是瓶颈。

**timeout**：33B 模型采样很慢，`config.ini` 里建议 `timeout = 2400` 起步。

---

## 7. 已交付内容（v1.3.0 / v1.3.1）

| 文件 | 内容 | 当前定位 |
|---|---|---|
| `core/latent.py` | 复合 latent 解析 / 保存，兼容 `latent_0`/`latent_1` 键名 | 本地用；若云端某天能出 AV latent，插件已能接住 |
| `nodes.py` | 3 个节点：RH AV Latent 拆分 / 合并 / 保存 | 本地用（自建云端 / 其他平台 / 本地跑 H3 时） |
| `extras/av_latent_nodes.py` | 零依赖单文件节点包（保存 + 拆分） | **RunningHub 上装不了**；留给允许装节点的环境 |
| `examples/05_minimax_h3_av.json` | 本地解码示例链路 | 仅限路线 A 可用时 |
| 自测 | 146 项全通过 | |

插件输出分类也兼容了社区包 `JerryZRic/comfyui-minimax-h3-latent` 的 `.h3latent` 扩展名：如果哪天云端预装了它，本插件可以直接识别解析。

---

## 8. 花 2 分钟做的低成本验证（可选）

万一 RunningHub 的 H3 适配里带了能存 latent 的节点，路线 A 就能复活。验证方法：

1. 在云端节点搜索框里依次搜这几个关键词，看有没有非原生的 latent 保存节点：
   `latent` · `h3` · `AV Latent` · `minimax` · `保存Latent`（看它的类别是否属于某个自定义节点包）
2. 建个最小 H3 工作流（124 帧 ≈ 5 秒，最短时长），末端接平台自带的「保存Latent」，跑一次：
   - 结果里出现 `.latent` / `.h3latent` 文件 → **路线 A 复活**，按第 4 节旧版步骤走，本地打开 `examples/05_minimax_h3_av.json`；
   - 报 `NestedTensor` / `contiguous` 相关错误 → 确认封死，安心走路线 C。

这一步同时验证两件事：平台有没有能用的保存节点、平台收不收录。成本就是一次最短时长的任务。

---

## 9. 排障清单

| 现象 | 原因 | 处理 |
|---|---|---|
| 云端报 `'NestedTensor' object has no attribute 'contiguous'` | 原生保存Latent 不支持复合 latent | 走路线 C，改用 VAEDecode + VAEDecodeAudio |
| 云端报 `KeyError: 'samples'` | 上游根本没产出 latent（如 InfiniteTalk 流式模式） | 该工作流无 latent 可存，只能云端出片 |
| 出片没声音 | 只接了 VAEDecode，音频被静默丢弃 | 显式加一路 VAEDecodeAudio 并保存 |
| 音画不同步 | 成片帧率填错 | H3 填 **24**（不是 16） |
| 音频采样率不对 | 音频 VAE 装错 | 用 H3 专用 audio VAE（32kHz） |
| 视频/音频 VAE 混用 | 两者不同，不能互换 | VAELoader 分别加载（`sd.py:1028` 视频 / `1059` 音频） |
| 本地接到 latent 却只有单个形状 | 云端存的不是复合 latent | 检查云端末端节点是否输出了完整 AV latent |
| 画面是噪声 | 误做了 0.18215 缩放补偿 | 复合 latent 固定不缩放（scale_factor=1.0） |

---

## 10. 合规提醒

H3 使用 **MiniMax H3 Community License**（非标准开源协议）：年营收 2000 万美元以下可免费商用，协议明确排除美英欧盟韩国（中国区可用）。商用前建议读一遍原文。
