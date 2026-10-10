"""[本地补丁] 起播延迟计时工具

把一次 AirPlay 会话的各阶段时间点打进日志，用来定位"按播放后要等几秒才出声"
究竟慢在哪一段：RTSP 握手 / 小米云往返 / MiAir 内部缓冲 / 音箱侧缓冲。

用法（各阶段代码里调 mark 即可）：

    _timing.begin("AirPlay 会话")      # 新会话开始，重置计时
    _timing.mark("首个 RTP 包")
    _timing.mark("命令音箱播放（交给小米云）")
    _timing.summary()                  # 可选：打印分段汇总

日志形如：

    [延迟] ===== AirPlay 会话 192.168.1.189 开始计时 =====
    [延迟] +       3 ms  (+      3 ms)  RTSP 连接
    [延迟] +     812 ms  (+    809 ms)  首个 RTP 包到达
    [延迟] +    2340 ms  (+   1528 ms)  音箱首次拉取 WAV 流

重要：**音箱"实际出声"的时刻在本机测不到**——取决于音箱自己攒流缓冲多久。
若日志显示首批 PCM 已经送出、而你听到声音明显滞后，那段延迟就在音箱侧，
不是 MiAir 能调的部分。

排查完成后可用 set_enabled(False) 关掉，减少日志噪音。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("miair")

_enabled = True
_t0: float | None = None
_marks: list[tuple[str, float]] = []


def set_enabled(flag: bool) -> None:
    """总开关。"""
    global _enabled
    _enabled = bool(flag)


def begin(label: str = "新会话") -> None:
    """开始一次新的计时。手机重新连接或换歌重建会话时都会重来一遍。"""
    global _t0, _marks
    _t0 = time.monotonic()
    _marks = []
    if _enabled:
        log.info(f"[延迟] ===== {label} 开始计时 =====")


def mark(name: str) -> None:
    """记录一个阶段时间点：输出距 begin 的累计毫秒，以及相对上一阶段的耗时。"""
    if not _enabled or _t0 is None:
        return
    now = time.monotonic()
    elapsed_ms = (now - _t0) * 1000.0
    delta_ms = (now - _marks[-1][1]) * 1000.0 if _marks else elapsed_ms
    _marks.append((name, now))
    log.info(f"[延迟] +{elapsed_ms:8.0f} ms  (+{delta_ms:7.0f} ms)  {name}")


def note(text: str) -> None:
    """插入一句不参与计时的说明。"""
    if _enabled:
        log.info(f"[延迟] ---- {text}")


def summary() -> None:
    """打印分段汇总，一眼看出最大的一段。"""
    if not _enabled or _t0 is None or not _marks:
        return
    total_ms = (_marks[-1][1] - _t0) * 1000.0
    log.info(f"[延迟] ===== 汇总：共 {total_ms:.0f} ms，{len(_marks)} 个阶段 =====")
    prev = _t0
    for name, ts in _marks:
        log.info(f"[延迟] {(ts - prev) * 1000.0:9.0f} ms   {name}")
        prev = ts
