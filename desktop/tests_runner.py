"""GenerationRunner 的编排测试：用假客户端跑完整流水线，不花钱、不碰 GPU。

覆盖的是**最容易出事、以前完全没测过**的部分：
  1. 正常出图：预校验 → 提交 → 轮询 → 取回 → 解码 → 落盘 → 写台账
  2. 轮询超时自动续等（已付费任务不该被本地计时器判死）
  3. 用户取消：要在开始前就拦住，并且台账记「canceled」而不是「failed」
  4. 映射预校验失败：必须在**花钱之前**就报错，不能提交
  5. 云端没有 latent 输出：要报错并且台账记失败

做法：把 core_bridge 的 client / cache / latent 换成假对象，把解码器和落盘
换成桩，于是整条链路只剩「编排逻辑」是真实的——正是要测的东西。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

from app.engine import core_bridge, embed as embed_mod, runner as runner_mod  # noqa: E402
from app.engine.runner import GenerationParams, GenerationRunner  # noqa: E402

FAKE_KEY = "k" * 32


# ---------------------------------------------------------------- 假实现
class FakeTimeout(Exception):
    """对应 core.client.TaskTimeoutError。"""


class FakeClientMod:
    TaskTimeoutError = FakeTimeout


class FakeClient:
    def __init__(self, api_key, *, outputs=None, timeouts=0):
        self.api_key = api_key
        self._outputs = outputs if outputs is not None else [
            {"nodeId": "9", "fileType": "latent", "fileUrl": "http://x/a.safetensors"}
        ]
        self._timeouts = timeouts          # 前 N 次轮询抛超时
        self.create_calls = 0
        self.poll_calls = 0
        self.workflow_id = ""

    def get_workflow_structure(self, workflow_id):
        self.workflow_id = workflow_id
        # 归一化后的结构：nodeId -> {class_type, inputs}
        return {"7": {"class_type": "KSampler", "inputs": {"seed": 1, "text": "hi"}}}

    def get_account_status(self):
        return {"remainCoins": 100}

    def create_task(self, workflow_id, node_info_list):
        self.create_calls += 1
        return "TASK-1"

    def wait_for_outputs(self, task_id, poll_interval=5.0, timeout=600.0, on_progress=None):
        self.poll_calls += 1
        if on_progress:
            on_progress("running", 12.0)
        if self._timeouts > 0:
            self._timeouts -= 1
            raise FakeTimeout("timeout")
        return self._outputs


class FakeCache:
    def download(self, url, timeout=120.0):
        return b"fake-blob"


class FakeTensor:
    """只要能冒充 torch.Tensor 到「形状判断」这一步就够。"""

    def __init__(self, shape):
        self.shape = shape

    def dim(self):
        return len(self.shape)

    def __getitem__(self, i):
        return self.shape[i]

    def tolist(self):
        return list(self.shape)


class FakeLatent:
    def parse(self, blob, url=""):
        return {"samples": FakeTensor((1, 16, 1, 8, 8))}  # 5 维但 F=1 → 图像

    def is_nested(self, obj):
        return False


class FakeDecoder:
    """解码器桩：不加载任何模型，直接吐一帧假数据。"""

    def __init__(self):
        self.loaded = []
        self.closed = False

    def load(self, path, on_stage=None):
        self.loaded.append(path)
        if on_stage:
            on_stage("dec_load_vae")

    def decode_images(self, samples, on_stage=None):
        return ["frame-1"]

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- 通用夹具
class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger: list = []
        self.progress: list = []

        # 每一项都是 patch，退出时统一还原
        self._patches = [
            patch.object(core_bridge, "core_client", lambda: FakeClientMod),
            patch.object(core_bridge, "core_cache", lambda: FakeCache()),
            patch.object(core_bridge, "core_latent", lambda: FakeLatent()),
            patch.object(
                core_bridge,
                "core_ledger",
                lambda: type("L", (), {"append_record": staticmethod(self.ledger.append)}),
            ),
            patch.object(embed_mod, "attach_png", lambda *a, **k: None),
            patch.object(runner_mod, "_output_dir", lambda: self.tmp.name),
            patch.object(
                runner_mod, "_save_frames",
                lambda frames, output_dir, task_id: [
                    os.path.join(output_dir, "out.png")
                ],
            ),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

        # 假 Key（不走真实配置）
        from app import appconfig

        self._appconfig_patch = patch.object(appconfig, "api_key", lambda: FAKE_KEY)
        self._appconfig_patch.start()
        self.addCleanup(self._appconfig_patch.stop)

    def _stop_patches(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def make_runner(self, client) -> GenerationRunner:
        """把 core_client 换成一个返回指定假 client 的模块桩。"""
        fake_module = type("M", (), {
            "RunningHubClient": staticmethod(lambda key: client),
            "TaskTimeoutError": FakeTimeout,
        })
        core_bridge.core_client = lambda: fake_module
        runner = GenerationRunner()
        runner._decoder = FakeDecoder()
        return runner

    def run_it(self, client, params=None, cancel=False):
        runner = self.make_runner(client)
        params = params or GenerationParams(workflow_id="WF1")
        return runner.run(
            params,
            progress=self.progress.append,
            cancel_check=lambda: cancel,
        )


# ---------------------------------------------------------------- 用例
class HappyPathTests(RunnerCase):
    def test_normal_image_run(self):
        client = FakeClient(FAKE_KEY)
        result = self.run_it(client)

        self.assertEqual(result.task_id, "TASK-1")
        self.assertEqual(result.kind, "image")
        self.assertEqual(result.files, [os.path.join(self.tmp.name, "out.png")])
        self.assertEqual(result.coins_used, 0.0)  # 前后余额都是 100
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.poll_calls, 1)
        self.assertEqual(len(self.ledger), 1)
        self.assertEqual(self.ledger[0]["outcome"], "success")
        self.assertEqual(self.ledger[0]["task_id"], "TASK-1")
        # 进度里必须出现提交与保存（key 形式，未翻译）
        self.assertIn("stage_submit", self.progress)
        self.assertIn("stage_save", self.progress)

    def test_missing_api_key_reports_clearly(self):
        from app import appconfig

        appconfig.api_key = lambda: ""
        with self.assertRaises(Exception) as ctx:
            self.run_it(FakeClient(FAKE_KEY))
        self.assertIn("API Key", str(ctx.exception))
        # 没有 Key 时不该产生任何台账（连任务都没提交）
        self.assertEqual(self.ledger, [])


class PollTests(RunnerCase):
    def test_timeout_then_retry_succeeds(self):
        """第一次轮询超时 → 自动续等 → 第二次成功；已付费任务不能被判死。"""
        client = FakeClient(FAKE_KEY, timeouts=1)
        result = self.run_it(client)

        self.assertEqual(client.poll_calls, 2)
        self.assertEqual(result.task_id, "TASK-1")
        self.assertIn("stage_retry", self.progress)
        self.assertEqual(self.ledger[0]["outcome"], "success")

    def test_timeout_exhausted_fails(self):
        """续等轮数用尽后要抛错，并记失败（不能假装成功）。"""
        client = FakeClient(FAKE_KEY, timeouts=99)
        with self.assertRaises(FakeTimeout):
            self.run_it(client)
        self.assertEqual(self.ledger[0]["outcome"], "failed")


class CancelTests(RunnerCase):
    def test_cancel_before_start(self):
        client = FakeClient(FAKE_KEY)
        with self.assertRaises(Exception):
            self.run_it(client, cancel=True)
        # 取消发生在提交前：一次都不该调 create_task
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(self.ledger[0]["outcome"], "canceled")

    def test_cancel_is_not_recorded_as_failure(self):
        """取消要单独记 canceled——对账时「失败」和「用户取消」含义完全不同。"""
        client = FakeClient(FAKE_KEY)
        with self.assertRaises(Exception):
            self.run_it(client, cancel=True)
        self.assertNotEqual(self.ledger[0]["outcome"], "failed")


class PrevalidateTests(RunnerCase):
    def test_bad_mapping_blocks_before_spending(self):
        """映射指向不存在的 nodeId：必须在提交前报错（否则白花一次 803 的往返）。"""
        client = FakeClient(FAKE_KEY)
        params = GenerationParams(
            workflow_id="WF1",
            mapping_json='[{"nodeId":"99","fieldName":"seed","fieldValue":1}]',
        )
        with self.assertRaises(Exception) as ctx:
            self.run_it(client, params=params)
        self.assertEqual(client.create_calls, 0, "预校验失败却提交了任务（会白扣费）")
        self.assertIn("803", str(ctx.exception))
        self.assertEqual(self.ledger[0]["outcome"], "failed")

    def test_empty_outputs_fail_loudly(self):
        """云端没返回 latent：要报错，不能静默返回空结果。"""
        client = FakeClient(FAKE_KEY, outputs=[{"nodeId": "1", "fileType": "png",
                                                "fileUrl": "http://x/a.png"}])
        with self.assertRaises(Exception) as ctx:
            self.run_it(client)
        self.assertIn("latent", str(ctx.exception))
        self.assertEqual(self.ledger[0]["outcome"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
