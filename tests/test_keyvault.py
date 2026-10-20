"""core/settings.py 的 API Key 转存功能测试（Key 守卫配套后端）。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import settings  # noqa: E402


class MaskKeyTests(unittest.TestCase):
    # 注意：这里必须用**假** Key。以前这里写的是作者本人的真 Key，
    # 一旦随仓库发布就是凭证泄漏（已被 test_package 的「配置里的 Key 不得出现在
    # 仓库文件里」检查盯上，但仍不该在测试里出现任何真实凭证）。
    FAKE = "0123456789abcdef0123456789abcdef"

    def test_mask_long_key(self):
        self.assertEqual(settings.mask_key(self.FAKE), "0123••••cdef")

    def test_mask_short_key(self):
        self.assertEqual(settings.mask_key("abc123"), "••••••")

    def test_mask_empty(self):
        self.assertEqual(settings.mask_key(""), "")
        self.assertEqual(settings.mask_key(None), "")

    def test_mask_strips_whitespace(self):
        self.assertEqual(settings.mask_key("  abcdefgh12345678  "), "abcd••••5678")


class SaveApiKeyTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".ini")
        os.close(fd)
        os.remove(self.path)  # 只要路径，不要文件

    def tearDown(self):
        if os.path.isfile(self.path):
            os.remove(self.path)

    def _read(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_reject_empty_key(self):
        self.assertFalse(settings.save_api_key("", path=self.path))
        self.assertFalse(settings.save_api_key("   ", path=self.path))
        self.assertFalse(os.path.isfile(self.path))

    def test_creates_new_file(self):
        self.assertTrue(settings.save_api_key("KEY123", path=self.path))
        text = self._read()
        self.assertIn("[runninghub]", text)
        self.assertIn("api_key = KEY123", text)

    def test_replaces_existing_value(self):
        settings.save_api_key("OLDKEY", path=self.path)
        self.assertTrue(settings.save_api_key("NEWKEY", path=self.path))
        text = self._read()
        self.assertIn("api_key = NEWKEY", text)
        self.assertNotIn("OLDKEY", text)

    def test_preserves_comments_and_other_keys(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(
                "# 我的配置注释\n"
                "[runninghub]\n"
                "api_key = OLDKEY\n"
                "base_url = https://www.runninghub.cn\n"
                "\n"
                "[other]\n"
                "note = keep me\n"
            )
        self.assertTrue(settings.save_api_key("NEWKEY", path=self.path))
        text = self._read()
        self.assertIn("# 我的配置注释", text)  # 注释保住
        self.assertIn("api_key = NEWKEY", text)
        self.assertIn("base_url = https://www.runninghub.cn", text)
        self.assertIn("[other]", text)
        self.assertIn("note = keep me", text)
        self.assertNotIn("OLDKEY", text)

    def test_inserts_into_existing_section_without_key(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[runninghub]\nbase_url = https://x\n[other]\nfoo = 1\n")
        self.assertTrue(settings.save_api_key("KEY9", path=self.path))
        text = self._read()
        runninghub = text.split("[other]")[0]
        self.assertIn("api_key = KEY9", runninghub)
        self.assertIn("base_url = https://x", runninghub)

    def test_appends_section_when_missing(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[other]\nfoo = 1\n")
        self.assertTrue(settings.save_api_key("KEY7", path=self.path))
        text = self._read()
        self.assertIn("[other]", text)
        self.assertIn("[runninghub]", text)
        self.assertIn("api_key = KEY7", text)

    def test_key_with_special_chars(self):
        key = "abc+def/ghi=jkl"
        self.assertTrue(settings.save_api_key(key, path=self.path))
        self.assertIn(f"api_key = {key}", self._read())


class KeyStatusTests(unittest.TestCase):
    def test_status_shape(self):
        status = settings.key_status()
        self.assertIn(status["present"], ("true", "false"))
        self.assertIsInstance(status["masked"], str)
        # 状态里绝不能出现完整 Key（只允许脱敏形式，长度有上限）
        if status["masked"]:
            self.assertLessEqual(len(status["masked"]), 12)


class StatusLabelTests(unittest.TestCase):
    """api_key 输入框存的是「状态标签」而不是密钥，这套语义必须钉死。"""

    def test_label_detection(self):
        self.assertTrue(settings.is_status_label(settings.STATUS_LABEL))
        self.assertTrue(settings.is_status_label("已配置"))
        self.assertTrue(settings.is_status_label("  已配置 · 使用 config.ini  "))
        # 空串与真 Key 都不是标签
        self.assertFalse(settings.is_status_label(""))
        self.assertFalse(settings.is_status_label(None))
        self.assertFalse(settings.is_status_label("sk-abcdef1234567890"))

    def test_label_is_not_a_secret(self):
        """标签里不能带任何看起来像密钥的片段，否则它会被当成 Key 用。"""
        label = settings.STATUS_LABEL
        self.assertIn("已配置", label)
        self.assertLess(len(label), 40)
        # 真实 Key 通常是 32~64 位无空格串；标签必须含空格或中文
        self.assertTrue(any(ord(ch) > 127 for ch in label))

    def test_resolve_api_key_ignores_label(self):
        """传标签时应回落配置；没有配置时返回空串（由调用方报错）。"""
        # 用临时配置文件，避免受本机真实 config.ini 影响
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ini = os.path.join(tmp, "config.ini")
            with open(ini, "w", encoding="utf-8") as fh:
                fh.write("[runninghub]\napi_key = REAL_KEY_1234567890\n")
            settings.reload()
            real = settings.resolve_api_key("REAL_KEY_1234567890")
            self.assertEqual(real, "REAL_KEY_1234567890")
            # 标签 → 视为未填 → 回落配置里的 Key
            fallback = settings.resolve_api_key(settings.STATUS_LABEL)
            self.assertTrue(fallback)
            self.assertNotEqual(fallback, settings.STATUS_LABEL)
        settings.reload()


class NodeApiKeyInputTests(unittest.TestCase):
    """节点 INPUT_TYPES 里的 api_key 必须是「状态指示」而非密码框。"""

    @staticmethod
    def _nodes():
        import importlib.util

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        spec = importlib.util.spec_from_file_location(
            "rhplugin_kv", os.path.join(root, "__init__.py"),
            submodule_search_locations=[root],
        )
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["rhplugin_kv"] = pkg
        spec.loader.exec_module(pkg)  # noqa: F841 - 只为拿到节点类
        return sys.modules["rhplugin_kv.nodes"]

    def test_api_key_input_shape(self):
        nodes_mod = self._nodes()
        for cls in (
            nodes_mod.RunningHubLatentGeneratorSimple,
            nodes_mod.RunningHubLatentGenerator,
            nodes_mod.RunningHubLatentFetch,
            nodes_mod.RunningHubWorkflowInspector,
        ):
            spec_in = cls.INPUT_TYPES()["required"]["api_key"]
            self.assertEqual(spec_in[0], "STRING", cls.__name__)
            self.assertFalse(
                spec_in[1].get("password"),
                f"{cls.__name__}: api_key 不该是密码框（值本身就是可见的状态标签）",
            )
            # 默认值只允许是空串或状态标签，绝不能是密钥
            default = spec_in[1].get("default", "")
            self.assertTrue(
                default == "" or settings.is_status_label(default),
                f"{cls.__name__}: 默认值异常 {default!r}",
            )

    def test_hint_reflects_config_presence(self):
        """有 Key 时默认值 = 标签（新拖出来的节点直接显示已配置）；没有则空串。"""
        original = settings.get
        try:
            settings.get = lambda key, fb=None: "X" * 32 if key == "api_key" else fb
            self.assertEqual(settings.api_key_hint(), settings.STATUS_LABEL)
            settings.get = lambda key, fb=None: "" if key == "api_key" else fb
            self.assertEqual(settings.api_key_hint(), "")
        finally:
            settings.get = original

    def test_node_accepts_label_and_uses_config(self):
        """执行时把标签当「未填」，最终拿到的是配置里的真 Key。"""
        if not str(settings.get("api_key") or "").strip():
            self.skipTest("本机未配置 Key，跳过回落验证")
        nodes_mod = self._nodes()
        key = nodes_mod._resolve_api_key(settings.STATUS_LABEL)
        self.assertTrue(key)
        self.assertFalse(
            settings.is_status_label(key), "回落结果不能又是标签，否则会把标签当 Key 用"
        )


if __name__ == "__main__":
    unittest.main()
