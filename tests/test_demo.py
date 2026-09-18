"""端到端演示脚本回归测试。"""

import contextlib
import io
import unittest

from app.demo import main


class DemoTest(unittest.TestCase):
    def test_full_cycle_demo_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main()
        out = buf.getvalue()
        self.assertIn("state=completed", out)
        self.assertIn("digest_mismatch", out)  # 中途曾停止扩散
        self.assertIn("== v1 digest: True", out)  # 回滚后设备回到 v1 内容


if __name__ == "__main__":
    unittest.main()
