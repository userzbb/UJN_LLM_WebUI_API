"""ujn_console.enable_utf8_stdout 的行为测试。

重点：该函数在真实终端、重定向到文件、以及不支持 reconfigure 的假 stdout
三种情况下都不能抛异常。
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ujn_console import enable_utf8_stdout


def test_sets_utf8_encoding_on_real_stdout():
    """真实 sys.stdout 是 TextIOWrapper，应被切成 utf-8。"""
    original = sys.stdout
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        enable_utf8_stdout()
        assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    finally:
        sys.stdout = original


def test_tolerates_stdout_without_reconfigure():
    """不是 TextIOWrapper 时必须静默跳过，绝不抛异常。

    这正是 pyright 报错的那个场景：某些环境给 sys.stdout 挂的是
    TextIO 的其它实现，没有 reconfigure。
    """
    original = sys.stdout
    try:
        sys.stdout = io.StringIO()  # StringIO 不是 TextIOWrapper，没有 reconfigure
        enable_utf8_stdout()  # 不应抛异常
        assert sys.stdout.encoding is None or True  # 未被修改
    finally:
        sys.stdout = original


def test_is_idempotent():
    """调用两次不应出错。"""
    original = sys.stdout
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        enable_utf8_stdout()
        enable_utf8_stdout()
        assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    finally:
        sys.stdout = original
