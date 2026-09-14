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
    """把 stdout 与 stderr 都切到 UTF-8（不支持时静默跳过）。

    在程序入口调用一次即可。重定向到文件或管道时同样有效。

    两个流都要切：警告走 stderr，若只切 stdout，在 cp1252/ascii 这类
    不含中文的代码页下打印中文警告会抛 UnicodeEncodeError —— 而警告通常
    出现在"主任务已成功"之后的路径上，异常冒泡会让调用方误判为整步失败。

    名字保留 stdout（既有调用点很多），但行为已覆盖 stderr。
    errors="replace" 而不是 strict：宁可输出降级成 '?'，也不要在
    报错路径上再抛一次异常。
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
