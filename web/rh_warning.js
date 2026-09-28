/**
 * ComfyUI-RunningHub-Latent 前端增强
 *
 * 两件事：
 *  1. 收到后端的警告事件时，把对应节点改黄并弹一条 toast。
 *     纯 Python 改不了节点颜色，必须靠前端配合——这也是「提示词没生效」
 *     这类问题唯一能在画布上被一眼看到的方式。
 *  2. 把本插件的提示词输入框拉高，方便编辑长文本。
 *
 * 本文件全程 try/catch 包裹：任何一步失败都只是少了视觉效果，
 * 绝不能因为前端报错导致整个 ComfyUI 画布打不开。
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EVENT = "runninghub-latent-warning";

// 本插件里用于填写提示词 / 参数映射的字段名
const TALL_FIELDS = ["positive", "negative", "node_overrides"];

function toast(message) {
    try {
        if (app.extensionManager && app.extensionManager.toast) {
            app.extensionManager.toast.add({
                severity: "warn",
                summary: "RunningHub 警告",
                detail: message,
                life: 12000,
            });
            return;
        }
    } catch (e) {
        /* 老版本前端没有 toast，忽略 */
    }
    console.warn("[RunningHub-Latent]", message);
}

function markNode(nodeId, message) {
    if (!nodeId) return;
    let node = null;
    try {
        node = app.graph.getNodeById(nodeId);
    } catch (e) {
        node = null;
    }
    if (!node) return;

    try {
        // 保存原色，方便后续恢复
        if (node.rhOrigColor === undefined) {
            node.rhOrigColor = node.color;
            node.rhOrigBg = node.bgcolor;
        }
        node.color = "#7a5c00";
        node.bgcolor = "#ffe9a8";
        node.rhWarning = message;
        if (typeof node.setDirtyCanvas === "function") {
            node.setDirtyCanvas(true, true);
        } else {
            app.graph.setDirtyCanvas(true, true);
        }
    } catch (e) {
        console.warn("[RunningHub-Latent] 节点上色失败", e);
    }
}

function restoreNode(node) {
    try {
        if (node.rhOrigColor === undefined) return;
        node.color = node.rhOrigColor;
        node.bgcolor = node.rhOrigBg;
        node.rhWarning = null;
        if (typeof node.setDirtyCanvas === "function") {
            node.setDirtyCanvas(true, true);
        }
    } catch (e) {
        /* 忽略 */
    }
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
                    markNode(String(detail.node_id || ""), detail.message || "");
                    if (detail.message) toast(detail.message);
                } catch (e) {
                    console.warn("[RunningHub-Latent] 警告事件处理失败", e);
                }
            });
        } catch (e) {
            console.warn("[RunningHub-Latent] 事件监听注册失败", e);
        }
    },

    async nodeCreated(node) {
        try {
            const name = node && node.constructor && node.constructor.type;
            if (typeof name === "string" && name.indexOf("RunningHub") === 0) {
                enlarge(node);
            }
        } catch (e) {
            /* 忽略 */
        }
    },

    async beforeRegisterNodeDef(nodeType, nodeData) {
        try {
            // 节点被重新执行时恢复原色，避免旧警告一直挂着误导用户
            const onExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                restoreNode(this);
                return onExecuted ? onExecuted.apply(this, arguments) : undefined;
            };
        } catch (e) {
            /* 忽略 */
        }
    },
});
