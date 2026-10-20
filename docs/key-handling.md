# API Key 的存放与显示（设计决策记录）

> 状态：已实现（v1.3.1+）｜ 相关代码：`core/settings.py`、`nodes.py:_api_key_input`、
> `web/rh_key_guard.js`、`__init__.py:_register_keyguard_routes`

## 一、要同时满足的两条硬约束

1. **导出的东西不能含 Key** —— 工作流 JSON、API 格式、PNG 元数据、mp4 元数据，
   只要可能被分享出去，就不允许出现密钥。
2. **自己用不能每次都重填** —— 重启 ComfyUI、重新加载工作流之后，
   节点上要能一眼看出「已经配好了」，而不是一片空白让人以为丢了。

这两条本身不矛盾，但**实现顺序**很容易做错：如果先把 Key 放进 widget 再想办法
在导出时抹掉，就永远在跟序列化路径赛跑。

## 二、试过并被否决的方案

### 方案 A：Key 存 widget，导出时清空（v1.3.0 早期）

widget.value = 真 Key，前端拦截 `graphToPrompt` / `graph.serialize` 把字段清空。

- ❌ **否决原因**：序列化出口不止两个。子图、剪贴板、第三方节点、
  前端版本升级新增的路径……漏一个就是密钥泄漏，而且漏了没人会发现。
- ✅ 保留的部分：这套拦截仍然留着，作为**双保险**（见第五节），
  但不再承担「Key 只存这里」的职责。

### 方案 B：widget 留空 + 视图层画脱敏圆点（v1.3.1 尝试）

widget.value 恒为 `""`，靠 `inputEl.value = "7ddb••••f204"` 在视图层画圆点，
再包装 `widget.draw()` 每次重绘后补一次。

- ❌ **否决原因**：这是在跟渲染器抢同一块 DOM。ComfyUI 每次重绘都会把
  `inputEl.value` 同步回 `widget.value`（空串），异步补写必然被覆盖；
  新旧前端的 `inputEl` 是否存在还不一样。
  用户实测表现就是**「重启 / 重载工作流后又是空的」**——改两轮都没治好。
- 教训：**不要跟框架的渲染管线对抗**。凡是「框架渲染 A，我在旁边偷偷改成 B」
  的方案，都只会在某个版本上碰巧能用。

### 方案 C：干脆删掉 api_key 字段，只读 config.ini

- ❌ **否决原因**：ComfyUI 里删除 `required` 字段会让所有历史工作流报
  「缺少必需输入」；用户也失去了「这个节点到底配没配」的可视信号。

## 三、采用的方案：widget 里放「无害状态标签」

核心思路换了个方向：**不要去遮一个空值，而是让这个值本身就没有保密的必要。**

```python
# core/settings.py
STATUS_LABEL = "已配置 · 使用 config.ini"
```

| 状态 | widget.value | placeholder |
|---|---|---|
| 已配置 | `已配置 · 使用 config.ini` | `已配置（7ddb••••f204）· 粘贴新 Key 可覆盖` |
| 未配置 | `""` | `未配置：右键本节点 →「设置 API Key」` |

三个关键点：

1. **标签由服务端生成**（`INPUT_TYPES` 里 `default=api_key_hint()`）。
   `INPUT_TYPES` 在服务端执行，所以新拖出来的节点**不需要任何前端脚本**
   就显示正确状态。
2. **加载工作流时**由前端在 `onConfigure` 之后补一次
   （文件里存的是 `""` 或旧 Key，需要用实时状态覆盖）。
3. **执行时把标签当「未填」**：`settings.resolve_api_key()` 识别标签后回落
   config.ini。所以标签进不进 prompt 都无所谓，功能始终正确。

### 为什么这样导出就一定干净

- 标签不是秘密，**即使某条序列化路径漏了拦截，泄漏的也只是「已配置」四个字**；
- 真正的 Key 只存在于 `config.ini`，从来没进过图；
- 原有的前端清空 + 服务端剥离仍然保留（见下），导出里连标签都会被清成 `""`。

这就是「先保证安全，再谈显示」和「先显示，再想办法藏」的区别。

## 四、用户怎么设置 Key

| 路径 | 说明 |
|---|---|
| **右键节点 →「设置 API Key」** | 推荐。浏览器原生输入框，完全不经过画布控件，天然不进任何序列化路径 |
| 直接粘在节点输入框里 | 允许，但失焦时会被前端**转存进 config.ini 并把框换回标签**——真 Key 存活时间不到一秒 |
| 手改 `config.ini` | `[runninghub] api_key = xxx`，适合批量部署 |

打开旧工作流时若框里残留真 Key，`onConfigure` 里会自动转存并替换成标签。

## 五、仍然保留的两道防线

1. **前端出口清空**（`web/rh_key_guard.js`）：`graphToPrompt` / `LGraph.serialize`
   两个出口把 `api_key` 清成 `""`（含子图）。注意这里**只转存「像真 Key」的值**，
   否则一次提交就会把 config.ini 里的真 Key 覆盖成标签文本。
2. **服务端剥离**（`__init__.py:_install_prompt_key_guard`）：用官方钩子
   `PromptServer.add_on_prompt_handler` 在入队最前端原位剥离 `api_key` 与
   `extra_pnginfo.workflow` 里对应位置。这层**不依赖浏览器 JS 是否活着**，
   覆盖「执行输入 + PNG 元数据」两条路径。

## 六、已知边界

- 前端脚本若完全没加载（浏览器缓存 / 禁用 JS / 前端大改），加载**旧**工作流时
  字段会是空的——但 placeholder 仍会显示「已配置（••••）」，
  且执行不受影响（会回落 config.ini）。这是有意的降级：宁可显示得朴素，
  也不能为了显示去藏密钥。
- `config.ini` 是明文。它对同级目录的进程可读，本插件不做加密——
  真正的隔离靠文件权限，加密只是把密钥挪到另一个本地文件，收益很低。

## 七、分享给别人时会发生什么

**结论：别人拿到的是一份「没有 Key」的插件，必须自己配一次，之后永久生效。**

| 问题 | 答案 | 机制 |
|---|---|---|
| 会继承我的 Key 吗？ | **不会** | Key 只在本机 `config.ini`；用 git 分享时被 `.gitignore` 排除 |
| 会用我的 Key 跑别人的任务吗？ | **不会** | 没有 `config.ini` → `api_key_hint()` 返回空 → 执行时直接报「没有拿到 API Key」 |
| 他们能自己配吗？ | **能** | 右键节点 →「设置 API Key」→ 写入他们自己的 `config.ini` |
| 配完会反复失效吗？ | **不会** | 与本机同一套逻辑：重启 / 重载工作流都显示「已配置」 |
| 配置失败会没反应吗？ | **不会**（v1.3.1+） | 插件目录只读时，前端会弹出错误并给出 `config.ini` 的完整路径 |

### 唯一的坑：用 zip 分享时 `config.ini` 会被带走

git clone / GitHub 发布不受影响（`.gitignore` 挡住了），
但**把目录压缩成 zip 发给别人时，本机的 `config.ini` 就在包里**。

分享前跑一次自检：

```bash
cd ComfyUI-RunningHub-Latent
python tools/check_share_ready.py        # 只检查
python tools/check_share_ready.py --fix  # 顺手清掉本机专属文件
```

`tests/test_package.py` 里还有一道常驻检查：
**本机配置的 Key 一旦出现在任何要发布的文件里（源码 / 文档 / 示例 / 测试）就报错**。
（这条是真事触发加的：`tests/test_keyvault.py` 里曾用作者本人的真 Key 当测试夹具，
虽然没提交进 git 历史，但只要推上去就是泄漏。）
