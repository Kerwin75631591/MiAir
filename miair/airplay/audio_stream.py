"""AirPlay 音频 HTTP 流输出

将解码后的 PCM 音频数据转换为 HTTP 音频流，供小爱音箱播放。
支持两种输出格式:
- MP3: 兼容性好，适用于 L05B/L05C/LX06/L16A 等不支持 WAV 的音箱
- WAV: 零编码延迟，直接输出 PCM，适用于大多数音箱
"""

import asyncio
import logging
import os
import queue
import struct
import subprocess
import threading
import time

from aiohttp import web

from miair import _timing

log = logging.getLogger("miair")

# --- [本地补丁] 起播延迟优化：虚报 WAV 头的 byte_rate（默认已启用）---
# 小爱音箱用 data÷byte_rate 推算"这路流有多长"来决定起播缓冲深度，而真正解码只用
# sample_rate/位宽/声道 —— 所以把 byte_rate 虚报得很大（让音箱以为流很短），就能几乎
# 不做起播缓冲；data_size 仍声明 0x7FFFFF00，因此永不截断、播放速度也不受影响。
# 实测（用户实听）：起播从 4s 降到约 1s，无断续、无忽快忽慢，音箱也不掉线重连。
#   MIAIR_STREAM_MODE=wav_br（默认）虚报 byte_rate
#   MIAIR_STREAM_MODE=wav          原始行为（逃生口：固件升级后若失效可用它退回）
# MIAIR_WAV_SECONDS：默认 30 —— 让音箱"以为"这路流有 30 秒。数值越小起播越快、缓冲越薄；
# 出现断续/爆音就调大（如 60/120），想更快可调小（如 1）。
# 注：曾实验过 wav_small（缩短 data → 起播快但硬截断）、wav_riff（缩小 riff_size →
# 破坏播放时序、声音突然加速）、l16（裸 PCM → LX06 不认、无声），均已删除代码。
# 注：曾想"去掉 Transfer-Encoding: chunked、改用 Connection: close 定界"，但实测
# aiohttp 3.14 对无长度的 HTTP/1.1 流强制分块（连私有 _chunked=False 都无效），未做。
_STREAM_MODE = os.environ.get("MIAIR_STREAM_MODE", "wav_br").strip().lower()
_WAV_SECONDS = int(os.environ.get("MIAIR_WAV_SECONDS", "30") or 30)

# --- 队列参数 ---
# 每个 ALAC 包约 8ms (352 samples @ 44100Hz)
# [本地补丁] 原值 100（≈800ms）会在"MiAir 已开始产出 PCM，但音箱还没来拉流"的窗口里
# 被填满，进而触发 write_pcm 的丢弃策略（丢最旧的 25%），把每首歌开头的约 200ms 削掉。
# 实测该窗口可达 828ms（命令下发 → 小米云往返 781ms → 音箱才开始拉流），且音箱慢时会更长，
# 因此提高到 400（≈3.2s）以完整覆盖这段等待。内存代价约 400 × 1.4KB ≈ 560KB。
# 注意：这不增加起播延迟——积压量由等待时长决定，与上限无关；上限只决定何时开始丢。
_QUEUE_MAXSIZE = 400

# [本地补丁] 队列溢出时一次丢弃的包数（固定值，不再按上限比例放大）。
# 25 包 ≈ 200ms：足够腾出空间，又不会像"上限 // 4"那样一次丢掉 800ms 造成明显停顿。
_DROP_PACKETS = 25


class AudioStreamServer:
    """HTTP 音频流服务器

    接收 PCM 音频数据，通过 HTTP 提供给小爱音箱播放。
    根据音箱型号选择 MP3 (ffmpeg 转码) 或 WAV (直接输出) 格式。
    """

    def __init__(self, hostname: str, port: int = 0, audio_format: str = "wav"):
        self.hostname = hostname
        self.port = port
        self._audio_format = audio_format  # "mp3" or "wav"
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # 音频数据队列 - 小队列 = 低延迟
        self._audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._sample_rate = 44100
        self._channels = 2
        self._sample_width = 2  # 16-bit
        self._active = False
        self._abort = False
        self._session_id = int(time.time())
        self._has_clients = False
        self._client_lock = threading.Lock()
        self._drop_events = 0  # [本地补丁] 卡顿诊断：队列溢出丢弃的累计次数

        self._setup_routes()

    def _setup_routes(self):
        if self._audio_format == "mp3":
            self._app.router.add_get("/airplay/stream.mp3", self._handle_stream_mp3)
        else:
            self._app.router.add_get("/airplay/stream.wav", self._handle_stream_wav)

    @property
    def stream_url(self) -> str:
        ext = "mp3" if self._audio_format == "mp3" else "wav"
        return f"http://{self.hostname}:{self.port}/airplay/stream.{ext}?sid={self._session_id}"

    async def start(self):
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]
        log.info(f"AirPlay 音频流服务器: http://{self.hostname}:{self.port} (格式: {self._audio_format})")

    async def stop(self):
        self._active = False
        try:
            self._audio_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._runner:
            await self._runner.cleanup()

    def set_audio_params(self, sample_rate: int, channels: int, sample_width: int = 2):
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_width = sample_width

    def start_streaming(self):
        self._active = True
        self._abort = False
        self._session_id = int(time.time())
        # 快速清空队列
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        log.info(f"音频流: 开始接收 PCM 数据 (格式: {self._audio_format})")

    def stop_streaming(self):
        self._active = False
        self._abort = True
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._audio_queue.put_nowait(None)
        except queue.Full:
            pass
        log.info("音频流: 停止接收 PCM 数据")

    def write_pcm(self, data: bytes):
        """写入 PCM 音频数据 — 非阻塞，队列满时丢弃一小批旧数据腾出空间"""
        if not self._active:
            return
        try:
            self._audio_queue.put_nowait(data)
        except queue.Full:
            # [本地补丁] 丢弃量固定为 _DROP_PACKETS。原实现用 _QUEUE_MAXSIZE // 4，
            # 上限从 100 提到 400 后单次丢弃会从 200ms 变成 800ms（明显停顿）。
            dropped = 0
            try:
                for _ in range(_DROP_PACKETS):
                    self._audio_queue.get_nowait()
                    dropped += 1
            except queue.Empty:
                pass
            try:
                self._audio_queue.put_nowait(data)
            except queue.Full:
                pass
            # [本地补丁] 卡顿诊断：溢出丢弃原本完全静默、日志里查不到，这里留痕
            if dropped:
                self._drop_events += 1
                log.warning(
                    f"[卡顿诊断] PCM 队列溢出，丢弃 {dropped} 包"
                    f"（≈{dropped * 8}ms），本会话累计 {self._drop_events} 次"
                )

    # ============================================================
    # WAV 模式 — 直接输出 PCM，零编码延迟
    # ============================================================

    def _reject_if_inactive(self) -> web.Response | None:
        """流已停止（TEARDOWN 后）返回 404 拒绝拉取，避免音箱把空响应当成 EOF
        反复重连该 URL 造成死循环（氛围灯闪烁、固件崩溃）。未停止返回 None。
        """
        if not self._active:
            log.info("AirPlay: 流已停止，拒绝拉取 (404)")
            return web.Response(status=404, headers={"Connection": "close"})
        return None

    def _build_wav_header(
        self,
        data_size: int = 0x7FFFFF00,
        byte_rate: int | None = None,
    ) -> bytes:
        """构造 44 字节 WAV 头。

        [本地补丁] 只把 byte_rate 做成可单独虚报（见文件头说明）。byte_rate=None 时
        与原实现逐字节一致。riff_size 仍写 data_size + 36——实测改小它会让音箱的
        播放时序错乱（声音突然加速），所以不开放该参数。
        """
        real_byte_rate = self._sample_rate * self._channels * self._sample_width
        block_align = self._channels * self._sample_width
        bits_per_sample = self._sample_width * 8
        if byte_rate is None:
            byte_rate = real_byte_rate
        return struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', data_size + 36, b'WAVE',
            b'fmt ', 16, 1, self._channels,
            self._sample_rate, byte_rate, block_align, bits_per_sample,
            b'data', data_size,
        )

    async def _handle_stream_wav(self, request: web.Request) -> web.StreamResponse:
        """WAV 模式: 直接输出 PCM 数据，零编码延迟

        使用专用写入线程从队列批量读取数据，通过 asyncio 事件写回 HTTP 响应，
        避免每个包都经过 asyncio.to_thread 的调度开销。
        """
        rejected = self._reject_if_inactive()
        if rejected is not None:
            return rejected

        stream_mode = _STREAM_MODE
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/wav",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Connection": "close",
                "Transfer-Encoding": "chunked",
            },
        )
        await response.prepare(request)

        # 关闭 Nagle 算法，让小包立即发送而不等待合并
        transport = request.transport
        if transport is not None:
            sock = transport.get_extra_info('socket')
            if sock is not None:
                import socket as _sock
                sock.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)

        with self._client_lock:
            self._has_clients = True
        self._abort = False  # 重置中断标志，允许续播

        _timing.mark("音箱开始拉取 WAV 流（音箱已响应云端命令）")
        log.info(f"AirPlay: 音箱开始拉取音频流 (零编码延迟) [流模式={stream_mode}]")

        # 使用 asyncio.Event 在写入线程和事件循环间通信
        loop = asyncio.get_event_loop()
        data_ready = asyncio.Event()
        pending_data: list[bytes] = []
        data_lock = threading.Lock()
        writer_done = False

        def _reader_thread():
            """专用线程：从队列批量读取 PCM 并通知事件循环"""
            nonlocal writer_done
            empty_streak = 0
            try:
                while self._active and not self._abort:
                    try:
                        chunk = self._audio_queue.get(timeout=0.02)
                        if chunk is None:
                            break
                        # 本地批量收集，避免每个 chunk 都加锁
                        local_batch = [chunk]
                        for _ in range(31):
                            try:
                                extra = self._audio_queue.get_nowait()
                                if extra is None:
                                    break
                                local_batch.append(extra)
                            except queue.Empty:
                                break
                        with data_lock:
                            pending_data.extend(local_batch)
                        loop.call_soon_threadsafe(data_ready.set)
                        empty_streak = 0
                    except queue.Empty:
                        empty_streak += 1
                        # 3 秒无数据写入静音保持连接
                        if empty_streak > 100:
                            silence = b'\x00' * (self._sample_rate * self._channels * self._sample_width // 50)
                            with data_lock:
                                pending_data.append(silence)
                            loop.call_soon_threadsafe(data_ready.set)
                        continue
            except Exception as e:
                log.error(f"WAV reader 异常: {e}")
            finally:
                writer_done = True
                loop.call_soon_threadsafe(data_ready.set)

        reader = threading.Thread(target=_reader_thread, daemon=True)
        reader.start()

        first_pcm_sent = False
        try:
            # [本地补丁] 默认虚报 byte_rate：让音箱以为这路流很短（几乎不做起播缓冲），
            # 而 data_size 保持巨大以保证永不截断。stream_mode="wav" 时用原始值。
            data_size = 0x7FFFFF00
            byte_rate = None
            if stream_mode == "wav_br":
                byte_rate = max(1, data_size // max(1, _WAV_SECONDS))
            await response.write(
                self._build_wav_header(data_size, byte_rate=byte_rate)
            )
            _timing.mark("WAV 头已写出")

            while not writer_done:
                await data_ready.wait()
                data_ready.clear()
                with data_lock:
                    chunks = pending_data
                    pending_data = []
                if chunks:
                    # 批量写入：合并所有 chunk 一次性写出
                    await response.write(b"".join(chunks))
                    if not first_pcm_sent:
                        first_pcm_sent = True
                        _timing.mark("首批 PCM 已送出（MiAir 侧到此为止）")
                        _timing.note(
                            "现在开始由音箱自行攒缓冲：若你听到声音明显晚于此刻，"
                            "延迟就在音箱侧，MiAir 无法再优化"
                        )
        except (ConnectionResetError, BrokenPipeError):
            log.info("AirPlay: 音箱断开 WAV 音频流连接")
        except Exception as e:
            pass
        finally:
            self._abort = True  # 通知 reader 线程退出
            with self._client_lock:
                self._has_clients = False

        try:
            await response.write_eof()
        except Exception:
            pass
        log.info("AirPlay: WAV 音频流结束")
        return response

    # ============================================================
    # MP3 模式 — ffmpeg 实时转码 (用于不支持 WAV 的音箱)
    # ============================================================

    async def _handle_stream_mp3(self, request: web.Request) -> web.StreamResponse:
        rejected = self._reject_if_inactive()
        if rejected is not None:
            return rejected

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        # 关闭 Nagle 算法
        transport = request.transport
        if transport is not None:
            sock = transport.get_extra_info('socket')
            if sock is not None:
                import socket as _sock
                sock.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)

        with self._client_lock:
            self._has_clients = True
        self._abort = False  # 重置中断标志，允许续播

        log.info("AirPlay: 音箱开始拉取 MP3 音频流")

        import os
        ffmpeg_bin = "ffmpeg"
        if os.path.exists("ffmpeg.exe"):
            ffmpeg_bin = os.path.abspath("ffmpeg.exe")
        elif os.path.exists("bin/ffmpeg.exe"):
            ffmpeg_bin = os.path.abspath("bin/ffmpeg.exe")

        ffmpeg_cmd = [
            ffmpeg_bin,
            "-loglevel", "error",
            "-fflags", "+nobuffer+flush_packets",
            "-flags", "low_delay",
            "-f", "s16le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-i", "pipe:0",
            "-acodec", "libmp3lame",
            "-compression_level", "0",  # 最快编码速度
            "-ab", "128k",
            "-ac", "2",
            "-ar", "44100",
            "-flush_packets", "1",
            "-id3v2_version", "0",
            "-f", "mp3",
            "pipe:1"
        ]

        try:
            proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            log.error("ffmpeg 未找到，无法转码音频流")
            with self._client_lock:
                self._has_clients = False
            await response.write_eof()
            return response

        def drain_stderr():
            try:
                while True:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    if line_str and 'rror' in line_str:
                        pass
            except Exception:
                pass

        threading.Thread(target=drain_stderr, daemon=True).start()

        def feed_ffmpeg():
            empty_streak = 0
            try:
                while True:
                    try:
                        chunk = self._audio_queue.get(timeout=0.05)
                        if chunk is None:
                            break
                        # 批量收集数据后一次性写入，减少 write 系统调用次数
                        chunks = [chunk]
                        for _ in range(15):
                            try:
                                extra = self._audio_queue.get_nowait()
                                if extra is None:
                                    proc.stdin.close()
                                    return
                                chunks.append(extra)
                            except queue.Empty:
                                break
                        proc.stdin.write(b"".join(chunks))
                        empty_streak = 0
                    except queue.Empty:
                        empty_streak += 1
                        if empty_streak > 40:
                            silence = b'\x00' * (self._sample_rate * self._channels * self._sample_width // 50)
                            try:
                                proc.stdin.write(silence)
                            except Exception:
                                break
                        continue
                    except (BrokenPipeError, OSError):
                        break
            except Exception as e:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=feed_ffmpeg, daemon=True).start()

        # 从 ffmpeg stdout 读取并写给客户端
        try:
            while self._active and not self._abort:
                audio_data = await asyncio.to_thread(proc.stdout.read, 8192)
                if not audio_data or self._abort:
                    break
                await response.write(audio_data)
        except (ConnectionResetError, BrokenPipeError):
            log.info("AirPlay: 音箱断开 MP3 音频流连接")
        except Exception as e:
            pass
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

        with self._client_lock:
            self._has_clients = False

        try:
            await response.write_eof()
        except Exception:
            pass
        log.info("AirPlay: MP3 音频流结束")
        return response
