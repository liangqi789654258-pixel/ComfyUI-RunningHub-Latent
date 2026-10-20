/**
 * ComfyUI-RunningHub-Latent 前端增强
 *
 * 四件事：
 *  1. 收到后端的警告 / 报错事件时弹 toast。
 *     ComfyUI 新版前端会把节点异常统一显示成「节点在执行过程中发生错误」，
 *     我们写的诊断文案在那儿出不来，只能自己开一条通道把它送到画布上。
 *  2. 把本插件的提示词输入框拉高，方便编辑长文本。
 *  3. API Key 守卫（防泄漏 + 状态可见）：
 *     - 显示层：api_key 输入框里放的是**状态标签**（「已配置 · 使用 config.ini」），
 *       不是密钥。标签是明文、无敏感信息，画布正常渲染即可，不需要遮罩技巧。
 *       （旧做法是在视图层画圆点 + 包装 widget.draw 补画，会被 ComfyUI 每次
 *        重绘同步覆盖，表现为「重启/重载后又是空的」。已废弃。）
 *     - 序列化层：在所有导出 / 提交出口（graphToPrompt、graph.serialize）
 *       把 api_key 字段清空 —— 保存工作流 JSON、导出 API 格式、
 *       生成图片的 PNG 元数据里都不会有 Key（标签也一起清掉，导出完全无痕）。
 *     - 存储层：Key 只存在插件目录 config.ini；用户粘贴的真 Key 会在失焦时
 *       转存并把框换回标签，所以它连一次序列化都撑不到。
 *  4. 兼容迁移：打开旧工作流时若框里残留真 Key，自动转存并替换成标签。
 *
 * 曾经做过「警告染黄 / 报错染红」的节点上色，用户反馈不喜欢，
 * 已移除——节点保持 ComfyUI 默认配色，诊断信息只走 toast。
 *
 * 本文件全程 try/catch 包裹：任何一步失败都只是少了视觉效果或少了保护，
 * 绝不能因为前端报错导致整个 ComfyUI 画布打不开。
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EVENT = "runninghub-latent-warning";
const ERROR_EVENT = "runninghub-latent-error";

// 本插件里用于填写提示词 / 参数映射的字段名
const TALL_FIELDS = ["positive", "negative", "node_overrides"];

// 本插件节点注册名前缀（RunningHubLatentGenerator / Fetch / FromUrl / ...）
const NODE_PREFIX = "RunningHub";

function toast(message, severity, summary) {
    try {
        if (app.extensionManager && app.extensionManager.toast) {
            app.extensionManager.toast.add({
                severity: severity || "warn",
                summary: summary || "RunningHub 警告",
                detail: message,
                // 报错比警告更需要时间读完，给足停留时间
                life: severity === "error" ? 60000 : 12000,
            });
            return;
        }
    } catch (e) {
        /* 老版本前端没有 toast，忽略 */
    }
    console.warn("[RunningHub-Latent]", message);
}

function isOurNode(node) {
    try {
        // 新版 TS 前端里 node.constructor 不一定挂 type（旧 litegraph 才有静态
        // type），实例上的 node.type 才是两代前端都可靠的注册名 ——
        // 这里曾只用 constructor.type，导致新版前端下整个节点级守卫静默失效。
        const name = (node && node.type) || (node && node.constructor && node.constructor.type);
        return typeof name === "string" && name.indexOf(NODE_PREFIX) === 0;
    } catch (e) {
        return false;
    }
}

function findKeyWidget(node) {
    try {
        if (!node || !node.widgets) return null;
        for (const w of node.widgets) {
            if (w && w.name === "api_key") return w;
        }
    } catch (e) { /* 忽略 */ }
    return null;
}

// ---------------------------------------------------------------------------
// Key 状态查询（本地后端只回「是否已配置 + 脱敏预览」，不回完整 Key）
// ---------------------------------------------------------------------------

let _keyStatusPromise = null;
function keyStatus() {
    if (!_keyStatusPromise) {
        _keyStatusPromise = api.fetchApi("/runninghub_latent/key_status")
            .then((r) => r.json())
            .catch(() => ({ present: "false", masked: "" }));
    }
    return _keyStatusPromise;
}

function placeholderText(status) {
    if (status && status.present === "true" && status.masked) {
        return "已配置（" + status.masked + "）";
    }
    return "粘贴 API Key（自动保存到 config.ini）";
}

// ---------------------------------------------------------------------------
// Key 转存：POST 给本地后端写入 config.ini
// ---------------------------------------------------------------------------

function saveKeyToConfig(key, onDone) {
    try {
        const trimmed = String(key || "").trim();
        if (!trimmed) return;
        api.fetchApi("/runninghub_latent/save_key", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: trimmed }),
        }).then((r) => r.json()).then((data) => {
            if (data && data.ok) {
                _keyStatusPromise = null; // 状态变了，作废缓存
                toast("API Key 已保存到 config.ini。工作流文件 / 导出 / 图片元数据均不含 Key。",
                      "success", "RunningHub");
                if (typeof onDone === "function") onDone(data.status);
                return;
            }
            // 保存失败必须说清楚，否则用户点了「设置」却毫无反馈。
            // 最常见原因是插件目录只读（装进 Program Files / 受管环境）。
            const where = (data && data.config_path) || "插件目录下的 config.ini";
            toast(
                "API Key 保存失败：无法写入配置文件。\n" + where
                + "\n\n请手工编辑该文件，加入：\n[runninghub]\napi_key = 你的Key",
                "error", "RunningHub · Key 未保存"
            );
        }).catch(() => { /* 后端不可用时静默，节点仍有「显式填写」路径 */ });
    } catch (e) { /* 忽略 */ }
}

// ---------------------------------------------------------------------------
// 序列化出口清空：工作流 JSON / API 格式 / PNG 元数据里不再有 Key
// ---------------------------------------------------------------------------

function widgetKeyIndex(node) {
    try {
        if (!node || !node.widgets) return -1;
        return node.widgets.findIndex((w) => w && w.name === "api_key");
    } catch (e) {
        return -1;
    }
}

/** 清空 API 格式 prompt 里的 api_key；返回收集到的待转存 Key（可能为空）。 */
function stripPromptKeys(promptData) {
    let collected = "";
    try {
        if (!promptData || !promptData.prompt) return collected;
        for (const nodeId of Object.keys(promptData.prompt)) {
            const item = promptData.prompt[nodeId];
            if (!item || typeof item.class_type !== "string") continue;
            if (item.class_type.indexOf(NODE_PREFIX) !== 0) continue;
            const inputs = item.inputs;
            if (!inputs || typeof inputs !== "object") continue;
            if (typeof inputs.api_key === "string" && inputs.api_key.trim()) {
                // 只把「像真 Key」的值收集起来转存；状态标签绝不能当 Key 存回去，
                // 否则一次提交就会把 config.ini 里的真 Key 覆盖成标签文本。
                if (!collected && looksLikeKey(inputs.api_key)) {
                    collected = inputs.api_key.trim();
                }
                inputs.api_key = "";
            }
        }
    } catch (e) { /* 忽略 */ }
    return collected;
}

/** 清空 UI 格式 workflow 节点数组里的 api_key widget 值。 */
function stripWorkflowKeys(workflowData) {
    try {
        if (!workflowData) return;
        const nodes = Array.isArray(workflowData.nodes) ? workflowData.nodes : [];
        // 用画布上的活节点确定 api_key 在 widgets_values 里的下标
        const liveIndex = new Map();
        const graphNodes = (app.graph && app.graph._nodes) || [];
        for (const n of graphNodes) {
            const idx = widgetKeyIndex(n);
            if (idx >= 0) liveIndex.set(String(n.id), idx);
        }
        for (const n of nodes) {
            if (!n || typeof n.type !== "string") continue;
            if (n.type.indexOf(NODE_PREFIX) !== 0) continue;
            if (!Array.isArray(n.widgets_values)) continue;
            const idx = liveIndex.has(String(n.id))
                ? liveIndex.get(String(n.id))
                : -1;
            if (idx >= 0 && idx < n.widgets_values.length) {
                n.widgets_values[idx] = "";
            }
        }
        // 子图（若前端版本支持）：逐个子图再走一遍
        const defs = workflowData.definitions;
        if (defs && typeof defs === "object") {
            const subgraphs = defs.subgraphs;
            if (Array.isArray(subgraphs)) {
                for (const sg of subgraphs) {
                    const inner = sg && Array.isArray(sg.nodes) ? sg.nodes : [];
                    for (const n of inner) {
                        if (!n || typeof n.type !== "string") continue;
                        if (n.type.indexOf(NODE_PREFIX) !== 0) continue;
                        if (!Array.isArray(n.widgets_values)) continue;
                        // 子图内部节点取第一个 widget 槽位兜底：
                        // 本插件所有节点的 api_key 都是 INPUT_TYPES 第一个字段
                        if (n.widgets_values.length > 0) {
                            n.widgets_values[0] = "";
                        }
                    }
                }
            }
        }
    } catch (e) { /* 忽略 */ }
}

function installSerializationGuards() {
    // 出口 1：graphToPrompt —— 队列提交 / PNG 元数据 / 「导出 API 格式」都走这里
    try {
        if (typeof app.graphToPrompt === "function" && !app.__rhKeyGuard) {
            const orig = app.graphToPrompt.bind(app);
            app.graphToPrompt = function () {
                let p;
                try {
                    p = orig();
                } catch (e) {
                    throw e;
                }
                try {
                    const key = stripPromptKeys(p);
                    if (key) saveKeyToConfig(key); // 提交兜底转存，防止 change 事件漏掉
                    stripWorkflowKeys(p.workflow);
                } catch (e) { /* 忽略 */ }
                return p;
            };
            app.__rhKeyGuard = true;
        }
    } catch (e) { /* 忽略 */ }

    // 出口 2：LGraph.serialize —— 老版前端「保存工作流」/ 剪贴板等路径
    try {
        const proto = app.graph && app.graph.constructor && app.graph.constructor.prototype;
        if (proto && typeof proto.serialize === "function" && !proto.__rhKeyGuard) {
            const origSerialize = proto.serialize;
            proto.serialize = function () {
                const data = origSerialize.apply(this, arguments);
                try {
                    stripWorkflowKeys(data);
                } catch (e) { /* 忽略 */ }
                return data;
            };
            proto.__rhKeyGuard = true;
        }
    } catch (e) { /* 忽略 */ }
}

// ---------------------------------------------------------------------------
// Key 状态标签：直接写 widget.value，不碰 DOM
//
// 上一版是在视图层（inputEl.value）画脱敏圆点、还包装了 widget.draw 补画。
// 那条路注定不稳：ComfyUI 每次重绘都会把 inputEl.value 同步回 widget.value
// （空串），异步补写会被覆盖；新旧前端的 inputEl 是否存在也不一样。
// 用户看到的现象就是「重启 / 重载工作流后又是空的」。
//
// 现在让 widget.value 本身就是一个**无害标签**：
//   有 Key → "已配置 · 使用 config.ini"；没 Key → 空串（placeholder 提示怎么配）
// 画布正常渲染它（不需要任何 DOM 技巧），序列化时仍由 stripXxx 清成空串，
// 所以导出的工作流 / 图片元数据里依旧没有 Key。
// ---------------------------------------------------------------------------

const STATUS_LABEL = "已配置 · 使用 config.ini";

function isStatusLabel(v) {
    const t = String(v || "").trim();
    return t === STATUS_LABEL || t.indexOf("已配置") === 0;
}

/** 像不像真的 API Key（有长度、无空白、不是标签）—— 决定要不要转存。 */
function looksLikeKey(v) {
    const t = String(v || "").trim();
    if (!t || t.length < 16) return false;
    if (/\s/.test(t)) return false;
    return !isStatusLabel(t);
}

function paintStatus(node, label) {
    try {
        const w = findKeyWidget(node);
        if (!w) return;
        if (String(w.value || "") === label) return;
        w.value = label;
        if (w.inputEl) w.inputEl.value = label; // DOM 在时顺手同步，省一次重绘闪烁
        if (typeof node.setDirtyCanvas === "function") {
            node.setDirtyCanvas(true, true);
        } else if (node.graph && node.graph.setDirtyCanvas) {
            node.graph.setDirtyCanvas(true, true);
        }
    } catch (e) { /* 忽略 */ }
}

function refreshKeyStatus(node) {
    if (!isOurNode(node)) return;
    const w = findKeyWidget(node);
    if (!w) return;

    const cur = String(w.value || "").trim();

    // 1) 框里是真 Key（老工作流残留 / 用户直接粘贴）→ 转存并立刻换回标签，
    //    这样它连一次序列化都撑不到。
    if (looksLikeKey(cur)) {
        saveKeyToConfig(cur, () => paintStatus(node, STATUS_LABEL));
        paintStatus(node, STATUS_LABEL);
        return;
    }

    // 2) 空串或已是标签 → 向后端问一次真实状态（Key 被清掉时要把标签撤掉）
    keyStatus().then((s) => {
        try {
            const present = s && String(s.present) === "true";
            paintStatus(node, present ? STATUS_LABEL : "");
            // placeholder 由 Python 侧按配置状态生成（见 settings.api_key_placeholder），
            // 这里不覆盖，避免两套文案打架。
        } catch (e) { /* 忽略 */ }
    });
}

function maskApiKey(node) {
    if (!isOurNode(node)) return;
    const widget = findKeyWidget(node);
    if (!widget) return;

    const hook = () => {
        try {
            if (!widget.inputEl || widget.__rhKeyHooked) return;
            widget.__rhKeyHooked = true;
            const onChange = () => {
                const v = String(widget.value || "").trim();
                if (!looksLikeKey(v)) return;
                saveKeyToConfig(v, () => paintStatus(node, STATUS_LABEL));
                paintStatus(node, STATUS_LABEL);
            };
            widget.inputEl.addEventListener("change", onChange);
            widget.inputEl.addEventListener("blur", onChange);
        } catch (e) { /* 忽略 */ }
    };

    hook();
    refreshKeyStatus(node);

    // 加载工作流：widget.value 要等 configure() 跑完才是文件里的值，
    // 所以这里再判定一次——老工作流里残留的真 Key 在这一步被转存并换回标签。
    try {
        const origConfigure = node.onConfigure;
        node.onConfigure = function (info) {
            if (origConfigure) {
                try { origConfigure.call(this, info); } catch (e) { /* 忽略 */ }
            }
            try { refreshKeyStatus(this); } catch (e) { /* 忽略 */ }
        };
    } catch (e) { /* 忽略 */ }

    // inputEl 首次绘制才创建，重试几次把事件挂上
    let tries = 0;
    const retry = () => {
        hook();
        if (widget.inputEl || tries >= 6) return;
        tries += 1;
        setTimeout(retry, tries <= 2 ? 200 : 600);
    };
    setTimeout(retry, 200);
}


function enlarge(node) {
    if (!node || !node.widgets) return;
    try {
        for (const w of node.widgets) {
            if (!w || !TALL_FIELDS.includes(w.name)) continue;
            if (w.type !== "customtext" && w.type !== "text") continue;
            if (w.rhResized) continue;
            const target = w.name === "node_overrides" ? 160 : 130;
            if (typeof w.computeSize === "function") {
                w.computeSize = ((orig) => {
                    return function (width) {
                        const size = orig ? orig.call(this, width) : [width, 40];
                        return [size[0], Math.max(size[1], target)];
                    };
                })(w.computeSize);
            }
            w.rhResized = true;
        }
    } catch (e) {
        console.warn("[RunningHub-Latent] 输入框高度调整失败", e);
    }
}

app.registerExtension({
    name: "runninghub.latent.warning",

    async setup() {
        try {
            api.addEventListener(EVENT, (event) => {
                try {
                    const detail = (event && event.detail) || {};
                    if (detail.message) toast(detail.message, "warn", "RunningHub 警告");
                } catch (e) {
                    console.warn("[RunningHub-Latent] 警告事件处理失败", e);
                }
            });
        } catch (e) {
            console.warn("[RunningHub-Latent] 事件监听注册失败", e);
        }

        try {
            api.addEventListener(ERROR_EVENT, (event) => {
                try {
                    const detail = (event && event.detail) || {};
                    const text = detail.message || "";
                    if (text) {
                        toast(text, "error", "RunningHub 执行失败");
                        console.error("[RunningHub-Latent] 执行失败\n" + text);
                    }
                } catch (e) {
                    console.warn("[RunningHub-Latent] 报错事件处理失败", e);
                }
            });
        } catch (e) {
            console.warn("[RunningHub-Latent] 报错事件监听注册失败", e);
        }

        installSerializationGuards();
    },

    async nodeCreated(node) {
        try {
            if (!isOurNode(node)) return;
            enlarge(node);
            maskApiKey(node);
            // 右键菜单：「设置 API Key」—— 浏览器原生输入框，完全不经过
            // 工作流控件，天然不进任何导出/序列化路径（比画布内的框更保险）
            const origMenu = node.getExtraMenuOptions;
            node.getExtraMenuOptions = function (canvas, options) {
                const r = origMenu ? origMenu.apply(this, arguments) : undefined;
                try {
                    options.push({
                        content: "设置 API Key（保存到 config.ini）",
                        callback: () => {
                            try {
                                const v = window.prompt(
                                    "粘贴 RunningHub API Key：\n（自动保存到插件 config.ini；"
                                    + "工作流文件 / 导出 / 图片元数据均不含 Key）",
                                    ""
                                );
                                if (v && v.trim()) {
                                    saveKeyToConfig(v.trim(), () => refreshKeyStatus(node));
                                }
                            } catch (e) { /* 忽略 */ }
                        },
                    });
                } catch (e) { /* 忽略 */ }
                return r;
            };
        } catch (e) {
            /* 忽略 */
        }
    },
});
