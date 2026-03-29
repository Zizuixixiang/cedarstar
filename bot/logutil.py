"""日志辅助：把异常写成可读的一行摘要（类型、说明、原因链），便于排查。"""

from __future__ import annotations

from typing import Any


def exc_detail(exc: BaseException, *, max_len: int = 1500) -> str:
    """
    生成适合写入日志的异常摘要。

    - 优先 ``str(exc)``；若为空则附带 ``repr(exc)`` 截断片段。
    - 沿 ``__cause__`` 最多追 5 层（根因常见于 SSLError、ConnectError 等包装异常）。
    """
    head = type(exc).__name__
    msg = str(exc).strip()
    if msg:
        head += f": {msg}"
    else:
        rep = repr(exc).strip()
        if rep and rep != head:
            head += f" ({rep[:480]})"

    bits: list[str] = [head]
    cur: Any = exc.__cause__
    depth = 0
    while cur is not None and depth < 5:
        cm = str(cur).strip() or repr(cur).strip()
        if len(cm) > 400:
            cm = cm[:397] + "..."
        bits.append(f"← {type(cur).__name__}: {cm}")
        cur = cur.__cause__
        depth += 1

    out = " | ".join(bits)
    if len(out) > max_len:
        return out[: max_len - 1] + "…"
    return out
