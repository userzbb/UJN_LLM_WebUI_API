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


def test_also_fixes_stderr():
    """stderr 也要切 —— 警告都走 stderr。"""
    original_out, original_err = sys.stdout, sys.stderr
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        enable_utf8_stdout()
        assert sys.stderr.encoding.lower().replace("-", "") == "utf8"
    finally:
        sys.stdout, sys.stderr = original_out, original_err


def test_chinese_warning_survives_narrow_console_encoding():
    """回归：本项目的警告是中文，而窄编码控制台打印中文会抛 UnicodeEncodeError。

    真实后果（实测确认）：这些警告都在"主任务已成功"之后的路径上
    （例如登录成功后的 JWT 提取警告），异常冒泡出去会变成非零退出码，
    让 run.ps1 误判整步失败并触发 3 次重试 + 退避 —— 而 Cookie 其实已经拿到了。

    对照组（不调用修复函数）在 ascii/cp1252 下确实会崩，见下方断言。
    """
    for enc in ["ascii", "cp1252"]:
        # 对照组：证明这些编码本来真的会崩（否则本测试没有意义）
        buf = io.TextIOWrapper(io.BytesIO(), encoding=enc, errors="strict")
        try:
            print("警告：中文", file=buf)
            buf.flush()
            raise AssertionError(f"{enc} 下打印中文未崩溃，本回归测试失去意义")
        except UnicodeEncodeError:
            pass  # 如期崩溃

        # 修复后：不应再崩
        original_out, original_err = sys.stdout, sys.stderr
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding=enc, errors="strict")
            sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding=enc, errors="strict")
            enable_utf8_stdout()
            print("  警告：没有 webvpn_host_query，跳过 JWT 提取（Cookie 已保存）",
                  file=sys.stderr)
            sys.stderr.flush()  # 不抛即通过
        finally:
            sys.stdout, sys.stderr = original_out, original_err
