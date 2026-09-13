"""控制台编码工具。

Windows 默认用 GBK 编码 stdout，打印中文或 emoji 时会抛 UnicodeEncodeError。
本模块提供一个小工具把 stdout 切到 UTF-8。

为什么不直接写 `if hasattr(sys.stdout, "reconfigure")`：
    typeshed 把 `sys.stdout` 标注为 `TextIO`，而 `TextIO` 并没有 `reconfigure`
    这个属性（只有 `io.TextIOWrapper` 才有）。所以静态检查器（pyright / Zed）
    会报 "Cannot access attribute reconfigure for class TextIO"。
    运行时是正确的，但为了类型检查也通过，这里做一次显式的 `TextIOWrapper` 转换。
"""

import io
import sys


def enable_utf8_stdout() -> None:
    """把 stdout 切到 UTF-8（不支持时静默跳过）。

    在程序入口调用一次即可。重定向到文件或管道时同样有效。
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
