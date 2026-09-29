# MiniMax H3：云端采样 + 本地解码

> 适用插件版本：v1.3.0+
> 本机核实环境：ComfyUI 0.37.0 / torch 2.13.0 / RTX 3060 6GB
> 文中所有行号与行为均在你本机 ComfyUI 源码上实测确认，非推测。

---

## 0. 结论先行

**H3 的 latent 存不了，不是你接线的问题，是 ComfyUI 原生 `SaveLatent` 节点没覆盖这种数据结构。**

解法很直接：

1. 云端把「保存Latent」换成 **AV Latent 保存** 节点（本插件 `extras/av_latent_nodes.py`，零依赖，单文件）；
2. 本地用本插件的 **RH AV Latent 拆分** 拆成视频 / 音频两条 latent，分别走 `VAEDecode` 和 `VAEDecodeAudio`；
3. 音频不会丢，而且可以在本地导出成独立音轨去剪辑。

**唯一还需要你确认的一环是：RunningHub 云端能不能安装自定义节点。** 这是整条链路目前唯一未验证的地方（见第 8 节）。

---

## 1. 为什么存不了：精确到行

### 1.1 H3 的 latent 不是一个张量

`comfy_extras/nodes_minimax_h3.py:84` 里 H3 创建空 latent 的代码是这样的：

```python
def _empty_av_latent(width, height, length, batch_size=1):
    video = torch.zeros([batch_size, 24, latent_t, height // 16, width // 16], ...)
    audio = torch.zeros([batch_size, 32, 2, audio_t], ...)
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count
```

也就是说 H3 的 latent 是 **一对张量** 打包成的 `NestedTensor`：

| 成员 | 形状 | 含义 |
|---|---|---|
| video | `[B, 24, T, H/16, W/16]` | 视频 latent，24 通道，空间压缩 16× |
| audio | `[B, 32, 2, T40]` | 音频 latent，32 通道，立体声 |

### 1.2 而 NestedTensor 是个纯 Python 容器

看了 `comfy/nested_tensor.py` 全文，它只有 `tensors` / `is_nested` / `unbind()` / `__add__` 这些，**没有任何张量方法**。

### 1.3 原生 SaveLatent 偏偏直接调用了张量方法

`nodes.py:552`：

```python
output["latent_tensor"] = samples["samples"].contiguous()
```

于是必然抛错。本机实测：

```
AttributeError: 'NestedTensor' object has no attribute 'contiguous'
```

### 1.4 佐证：别的节点都处理了，唯独 SaveLatent 没有

| 节点 | 位置 | 对复合 latent 的处理 |
|---|---|---|
| `VAEDecode` | `nodes.py:334` | `if latent.is_nested: latent = latent.unbind()[0]`（只解视频） |
| `VAEDecodeAudio` | `nodes_audio.py:100` | `if latent.is_nested: latent = latent.unbind()[-1]`（只解音频） |
| **`SaveLatent`** | **`nodes.py:552`** | **没有处理，直接崩** |

社区也是同样的结论——`JerryZRic/comfyui-minimax-h3-latent`（MIT）这个包存在的原因就是这一行。

---

## 2. 第二个坑（比第一个更容易踩）

就算你拿到了完整的复合 latent，**直接接 `VAEDecode` 会静默丢掉音频**。

`nodes.py:334` 只取 `unbind()[0]`（视频那一个），音频成员被丢掉，**不报任何错**。H3 最值钱的卖点就是原生立体声，这么一丢等于白跑。

所以本地**必须**先用「RH AV Latent 拆分」拆开：

- 视频 latent → `VAEDecode`（H3 视频 VAE）
- 音频 latent → `VAEDecodeAudio`（H3 音频 VAE），输出 `AUDIO`

顺带一个好消息：拆出来的两个都是**普通 torch.Tensor**，而 `torch.Tensor.is_nested` 存在且返回 `False`（本机 torch 2.13 实测），所以它们能直接喂给原生解码节点，不需要再包回 NestedTensor。

---

## 3. 三条路线怎么选

| 路线 | 云端要装新节点 | 音频结果 | 本地显存 | 适合 |
|---|---|---|---|---|
| **A 复合 latent 带回**（推荐） | 装「AV Latent 保存」 | latent 带回，无损，可本地导出音轨 | 需装 H3 视频 VAE（2.8–5.2GB） | 想本地解码 / 二遍处理 / 自己控 VAE |
| **B 云端拆开，音频云端解** | 装「AV Latent 拆分」 | 云端出 mp3 / flac 文件 | 同上（只解视频） | 只想拿音轨去剪辑，不想本地装音频 VAE |
| **C 云端整体出片** | 不需要 | 云端合成 | 0 | 不想折腾，直接要成片 |

**推荐 A**：一次把视频和音频 latent 都带回来，音频想解就解、想剪就导出，自由度最高。

**注意：即使 A/B 因为云端装不了节点而走不通，你要的「音频单独保存再剪辑」在路线 C 下照样能做**——云端 `VAEDecodeAudio` + `SaveAudioMP3` 就能导出独立音轨。这条不依赖任何新节点。

---

## 4. 路线 A：详细操作步骤

### 4.1 云端（RunningHub）

1. **准备节点文件**：`extras/av_latent_nodes.py`（本插件已提供，只用到 torch / safetensors / folder_paths，都是 ComfyUI 自带，无需 pip 安装）。安装途径见第 8 节。

2. **改工作流**：

   ```
   H3 采样器 → AV Latent 保存        ← 用这个替掉「保存Latent」
   ```

   两个要点：
   - 节点输出的扩展名我**故意沿用 `.latent`**（而不是社区包的 `.h3latent`）。因为云端平台是照着原生 SaveLatent 的产物来收录 latent 输出的，改扩展名有不被收录的风险。
   - **把 `VAEDecode` / `VHS_VideoCombine` 删掉或 bypass**，否则云端会白做一次完整解码，又慢又费额度。

3. **先小跑一次验证**：用最短的时长（124 帧 ≈ 5 秒），确认任务输出里有 `.latent` 文件可下载。这步不通过就别往下走。

### 4.2 本地

1. **下载两个 VAE 到 `models/vae/`**（`VAELoader` 能自动识别，见 `comfy/sd.py:1028` 视频 VAE、`1059` 音频 VAE）：

   | 文件 | 体积 | 说明 |
   |---|---|---|
   | H3 视频 VAE fp16 | ~5.2 GB | 画质完整 |
   | H3 视频 VAE int8 convrot | ~2.8 GB | 显存紧张时选这个 |
   | H3 音频 VAE | ~0.6 GB | 解音频必需 |

2. **打开 `examples/05_minimax_h3_av.json`**，把两个 VAELoader 的文件名换成你自己的，填上 api_key 和 workflow_id。

3. **链路**：

   ```
   RH 云端采样·专业版
       → RH AV Latent 拆分
           ├─ video → VAEDecode(H3 视频 VAE) ─────→ VHS_VideoCombine.images
           └─ audio → VAEDecodeAudio(H3 音频 VAE) → VHS_VideoCombine.audio
   ```

4. **看 report 确认**：
   - 显示 `张量形状：视频 (1, 24, 37, 48, 84) + 音频 (1, 32, 2, 207)` → AV 链路打通
   - 只显示单个形状 → 云端存的不是复合 latent，回去检查云端末端节点

---

## 5. 音频怎么单独拿出来剪辑

你提出「音频后面肯定还要剪辑」，三种做法都成立，按方便程度排：

**做法 1（最省事）：云端直接导出音轨**

```
云端：采样器 → VAEDecodeAudio(H3 音频 VAE) → SaveAudioMP3 / SaveAudio(FLAC)
本地：LoadAudio → VHS_VideoCombine.audio
```

不依赖任何新节点，路线 C 也能用。缺点是要自己下载音频文件再在本地加载。

**做法 2（路线 A 自带）：本地从 latent 解出后导出**

```
本地：RH AV Latent 拆分 → audio → VAEDecodeAudio → SaveAudio / SaveAudioMP3 → 导出 wav/flac/mp3
```

音频以 latent 形式带回来是无损的，想什么时候解就什么时候解，也可以同一份 latent 解多次。

**做法 3：直接合成**

音频 latent 解码后直接接 `VHS_VideoCombine` 的 `audio` 输入，一步出带音轨的 mp4（示例工作流就是这么接的）。

三个做法都不冲突：可以既合成成片，又顺手存一份独立音轨备用。

**对齐提醒**：H3 输出音频是 **32kHz 立体声**，视频是 **24fps**。本地 `VHS_VideoCombine` 的帧率要填 **24**（不是 Wan 的 16），否则音画会不同步。

---

## 6. 本地解码的硬参数

| 项目 | 值 | 出处 |
|---|---|---|
| 视频 latent 通道 | 24 | `latent_formats.py:623` |
| 空间压缩 | 16× | `latent_formats.py:625` |
| latent 帧数 | `((F-5)//17)*5 + 2`（F 为像素帧） | `nodes_minimax_h3.py:43` |
| 音频 latent | `[B,32,2,T40]`，T40 = 时长×40 | `nodes_minimax_h3.py:34/50` |
| **scale_factor** | **1.0**（不需要任何缩放补偿） | `latent_formats.py:627` |
| 帧率 | 24 | `nodes_minimax_h3.py:33` |
| 音频采样率 | 32 kHz | `nodes_minimax_h3.py:77` |

**关于 0.18215 那条缩放规则**：H3 的 `scale_factor` 就是 1.0，存的是原始值。插件在解析复合 latent 时**固定不做补偿**（`core/latent.py` 的 `parse_av`），你不用管，但要知道这条不能改——误乘 5.49 倍画面会直接毁成噪声。

**体积**：124 帧 / 768×1344 的视频 latent 约 `1×24×37×48×84` ≈ 358 万浮点数 ≈ **14MB**。传输从来不是瓶颈。

**timeout**：33B 模型采样很慢，`config.ini` 里建议 `timeout = 2400` 起步。

---

## 7. 本次已交付（v1.3.0）

| 文件 | 内容 |
|---|---|
| `core/latent.py` | 复合 latent 解析 / 保存；`.h3latent` `.avlatent` 键名兼容 |
| `nodes.py` | 新增 3 个节点：RH AV Latent 拆分 / 合并 / 保存；输出分类支持新扩展名；report 形状回显区分音视频 |
| `extras/av_latent_nodes.py` | **云端用**的零依赖单文件节点包（保存 + 拆分） |
| `examples/05_minimax_h3_av.json` | 可直接打开的本地解码示例 |
| 自测 | 130 → **146** 项全通过 |

插件输出分类也顺手兼容了社区包：如果云端装的是 `JerryZRic` 那个包（`.h3latent` 扩展名），本插件同样能识别并解析（键名 `latent_0` / `latent_1` 一致）。

---

## 8. 还需要你确认的一件事

**RunningHub 云端能否安装自定义节点？** 这是路线 A / B 的唯一前提。

可以试的三种途径：

1. 平台节点市场 / 插件管理里搜索安装（若已被收录）；
2. 把 `extras/av_latent_nodes.py` 放进一个小 GitHub 仓库，通过平台的自定义节点入口安装；
3. 找平台客服 / 提工单请求添加节点。

**建议先用最小成本验证**：云端建一个最小工作流（H3 或任何能跑的工作流都行），末端接「AV Latent 保存」，跑一次，看任务结果里有没有 `.latent` 文件。这一步能同时验证两件事——节点装上了没有、平台收不收录。

如果确认装不了：

- 视频 latent 这条路对 H3 **走不通**，退回路线 C（云端整体出片）；
- 但你的音频剪辑需求**仍然满足**：云端 `VAEDecodeAudio` + `SaveAudioMP3` 就能拿到独立音轨（做法 1）。

---

## 9. 排障清单

| 现象 | 原因 | 处理 |
|---|---|---|
| 云端报 `'NestedTensor' object has no attribute 'contiguous'` | 末端还在用原生「保存Latent」 | 换成 AV Latent 保存节点 |
| 云端跑通但本地 report 没有 latent | 平台未收录 `.latent` 输出 | 确认节点是 OUTPUT_NODE 且扩展名为 `.latent`；或改用社区包并确认平台收录 `.h3latent` |
| 本地「AV Latent 拆分」报「输入不是音视频复合 latent」 | 云端存的不是复合 latent | 检查云端末端节点；普通 latent 不需要拆分，直接接 VAEDecode |
| 画面正常但没有声音 | 没拆开就接了 VAEDecode | 音频被静默丢弃，必须先用拆分节点 |
| 音画不同步 | 帧率填错 | H3 是 24fps，VHS_VideoCombine 别用 16 |
| 画面糊成噪声 | 缩放补偿被误用 | 插件已固定对复合 latent 不补偿；确认没有走自定义后处理 |
| 本地解码 OOM | H3 视频 VAE 太大 | 换 int8 版（2.8GB）；或改用 `VAEDecodeTiled`（H3 VAE 自带 decode_tiled 实现，但未实测） |

---

## 10. 合规提醒

MiniMax H3 用的是 **MiniMax H3 Community License**（不是标准开源协议）：年营收 2000 万美元以下可免费商用，但协议明确排除美英欧盟韩国（中国区可用）。商用前请看一眼原文。

本插件只做「把自己的 latent 搬回本地解码」这一件事，不含任何规避平台检测的代码。
