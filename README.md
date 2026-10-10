# MiAir(修改版) - 为小爱音箱添加 DLNA 与 AirPlay 支持

> **本仓库是 [KiriChen-Wind/MiAir](https://github.com/KiriChen-Wind/MiAir) 的修改版 fork**，基于上游 `main` 分支
> 提交 `4416ffd`，在其之上修复了若干问题（主要是 **AirPlay 起播延迟**与**播放中途卡顿**）。
>
> - 原版使用文档、Docker 部署、Issue 讨论请前往 **上游仓库**
> - 本 README 只重点说明「本 fork 改了什么、怎么用、怎么调、怎么退回原版」
> - 所有改动都带 `[本地补丁]` 注释，且都留有开关或环境变量，**可随时退回原版行为**

## 引用以下开源项目代码 由衷感谢

**[XiaoMusic](https://github.com/hanxi/xiaomusic "XiaoMusic")** &ensp; **[AirPlay2 Receiver](https://github.com/openairplay/airplay2-receiver "AirPlay2 Receiver")** &ensp; **[MaCast](https://github.com/xfangfang/Macast "MaCast")**

## 本 fork 与原版的差异

实测环境：小爱音箱 Pro（**硬件型号 LX06**）、Windows 11、iPad 端 AirPlay 投音。

| 问题 | 原版 | 本 fork |
|---|---|---|
| **AirPlay 起播延迟** | 约 4 秒 | **约 3 秒**（调小 `MIAIR_WAV_SECONDS` 可更快） |
| **播放中途周期性卡顿** | 每几秒断一下 | **已消除**（音频零丢失） |
| 每首歌开头的音频 | 开头约 200ms 被丢弃 | 完整保留 |
| Windows 上的本机 IP | 可能显示一个不存在的地址 | 每次启动正确探测 |
| zeroconf 在 Windows 刷屏报错 | 持续抛 `WinError 1232` | 已消除 |
| 排查困难 | 关键事件无日志 | 新增 `[延迟]` 计时打点与 `[卡顿诊断]` 日志 |

### 起播延迟：4s → 3s

小爱音箱会用 WAV 头里的 `data ÷ byte_rate` 推算「这路流有多长」，并据此决定**起播前缓冲多深**。
而真正解码只依赖 `sample_rate` / 位宽 / 声道 —— 所以只要**虚报 `byte_rate`**（`data` 仍声明
`0x7FFFFF00`），音箱就会以为流很短、几乎不做缓冲，且**永不截断、播放速度也不受影响**。

默认已启用（让音箱以为流长 30 秒）。相关开关见下节。

> ⚠️ 这是**利用音箱固件解析器的宽松性**。若某次固件升级后不再读该字段，效果会自然退回约 4 秒
> —— **不会损坏设备、不会报错**。此时设 `MIAIR_STREAM_MODE=wav` 即可保持原版行为。

### 播放中途卡顿：根因与修复

原版 `PlaybackPacer` 在音帧迟到超过 **100ms** 时直接**丢弃该帧**（一帧 = 8ms 音频，丢掉就是一次断音）。
实测日志显示迟到量**稳定在 100~110ms**（刚好越过阈值一点点），于是每几秒就掉一次音。

修复方式：迟到时**重设锚点并照常播出**（限速 0.5 秒一次），时序照样被校正（延迟不会累积），
但**一帧都不丢** —— 只剩一次听不出来的微小时序调整。

### 其他修复

- **Windows IP 探测**：每次启动重新探测，不再沿用 `config.json` 里可能已过期的 `hostname`；
  并修了 `ipconfig` 解析中「空值属性行被误判成网卡名」导致虚拟网卡降权失效的问题。
- **zeroconf**：强制 IPv4-only 并尽量绑定到配置的那张网卡。原版 `IPVersion.All` 会在 Windows 上
  额外建立 7 个 IPv6 socket，在没有 IPv6 路由的接口上持续抛 `WinError 1232`。
- **PCM 队列**：上限 100 → 400（避免音箱还没来拉流就丢掉开头音频），单次丢弃量固定为
  25 包（约 200ms），不再随上限比例放大。
- **诊断能力**：新增 `miair/_timing.py`（起播链路计时），并给三处原先完全静默的事件补上日志
  —— PCM 队列溢出、Pacer 迟到、丢包重传放弃插静音。

## 快速开始（Windows）

*确保已安装 Python 3.12+*

```bash
git clone https://github.com/Kerwin75631591/MiAir.git
cd MiAir
python miair.py
```

首次运行会自动安装依赖（`aiohttp` / `miservice-fork` / `zeroconf` / `pycryptodome` / `av`），请保证网络畅通。

启动后访问 `http://主机IP:8300` 打开 Web 管理界面：填入小米账号（或 cookie/passToken）、选择音箱设备。
**Web UI 与 DLNA/AirPlay 服务的启动顺序**：Web 界面总是先启动；只有配置了账号 + 选中设备后，
DLNA(8200) 与 AirPlay 才会启动。

### ⚠️ ffmpeg 需要放在项目根目录

部分机型（**LX06 / L05B / L05C / L16A**）不支持无损格式，DLNA 播放无损音频时需要转码；
Seek 与部分格式处理也会用到 ffmpeg。

请把 `ffmpeg.exe`（Windows）放在**项目根目录**（与 `miair.py` 同级），否则上述功能会失败。
本仓库**未收录** ffmpeg 二进制（体积过大），可从
[官方 Release 包](https://github.com/KiriChen-Wind/MiAir/releases) 中取出，或自行下载。

> `ffmpeg` 是 GPLv3 授权的第三方程序，使用与分发请遵守其许可证。

## 可调开关（环境变量）

| 变量 | 默认 | 作用 |
|---|---|---|
| `MIAIR_STREAM_MODE` | `wav_br` | `wav_br` = 虚报 byte_rate（起播快，本 fork 默认）；`wav` = **原版行为**（起播约 4 秒） |
| `MIAIR_WAV_SECONDS` | `30` | 让音箱「以为」的流长度（秒）。**越小起播越快、缓冲越薄**；出现断续/爆音就调大（如 60/120），想更快可调小（如 1） |
| `MIAIR_HOSTNAME` | 空 | 显式指定本机局域网 IP。多网卡/有虚拟网卡时建议设成真实网卡地址（如 `192.168.1.223`） |
| `MI_USER` / `MI_PASS` / `MI_DID` | 空 | 小米账号 / 密码 / 设备 DID，仅在配置文件里对应字段为空时生效 |

Windows 下设置示例：

```powershell
$env:MIAIR_STREAM_MODE = 'wav'        # 退回原版行为
$env:MIAIR_WAV_SECONDS = '60'         # 缓冲厚一点，弱网更稳
python miair.py
```

### 起播慢或卡顿，怎么调？

| 现象 | 调整 |
|---|---|
| 起播还是 4 秒左右 | 确认终端里没有残留的 `MIAIR_STREAM_MODE=wav`；看日志里是否出现 `[流模式=wav_br]` |
| 播放中偶尔断一下 | 缓冲太薄 → `MIAIR_WAV_SECONDS` 调到 `60` 或 `120` |
| 想尽量快、网络很好 | `MIAIR_WAV_SECONDS=1`（可进一步缩短起播，实际数值随环境而异） |
| 声音忽快忽慢 | 属异常，请提 Issue 并附日志 |

## 诊断日志

遇到问题时日志里会有这两类前缀，便于定位：

```
[延迟] ===== AirPlay 会话 192.168.1.75 开始计时 =====
[延迟] +       0 ms  (+      0 ms)  RTSP 连接建立
[延迟] +     219 ms  (+    219 ms)  FairPlay+SDP 解析完成、解码器就绪
[延迟] +     485 ms  (+    266 ms)  MiAir 起播缓冲满足（开始向音箱送 PCM）
[延迟] +     610 ms  (+    125 ms)  小米云 play_url 返回成功
[延迟] +     657 ms  (+     47 ms)  音箱开始拉取 WAV 流
[延迟] +     657 ms  (+      0 ms)  首批 PCM 已送出（MiAir 侧到此为止）
```

> 注意：音箱**实际出声**的时刻本机测不到 —— 若你听到声音明显晚于「首批 PCM 已送出」，
> 那段延迟在音箱侧（它自己攒缓冲），不是 MiAir 能优化的部分。

```
[卡顿诊断] Pacer 迟到 101ms → 已改为重锚点（不再丢帧）；距上次 D4 锚点 1885ms，累计重锚 3 次
[卡顿诊断] PCM 队列溢出，丢弃 25 包（≈200ms），本会话累计 1 次
[卡顿诊断] 重传放弃 seq=6789，插入静音 5 帧（≈40ms），累计 2 次
```

这三条**只有在异常时才会出现**。若频繁出现，说明缓冲需要调厚（见上节）。

## 常见问题

**DLNA/AirPlay 搜不到设备？**
1. 先看日志里 `主机名` 是不是**真实的局域网 IP**（不是虚拟网卡/虚拟机的地址）。不对就设 `MIAIR_HOSTNAME`
2. **DLNA 发现失败常见原因是 UDP 1900 被占用** —— qBittorrent 等带 UPnP/NAT-PMP 端口的软件会抢占 SSDP 端口，请关闭后重试
3. 电脑与音箱必须在同一局域网；端口 8200（DLNA）与 8300（Web）不要被防火墙拦截

**日志显示「登录验证失败 / passToken 换取 serviceToken 失败」？**
cookie/passToken 已失效，请在 Web 界面重新登录。

**修改代码后不生效？**
必须**重启进程**。另外注意：程序每次启动都会**清空** `conf/miair.log`，需要留证据请先复制日志。

**配置和凭据存在哪？**
`conf/config.json`（含明文账号/cookie，注意不要外泄或提交），缓存的登录凭据在 `conf/.mi.token`。

## 与上游同步 / 退回原版

```bash
git remote add upstream https://github.com/KiriChen-Wind/MiAir.git   # 若尚未添加
git fetch upstream
git merge upstream/main        # 合入上游更新（有冲突时需自行解决）
```

不想用本 fork 的任何改动：把所有改动合起来只有 6 个文件，直接改用上游仓库即可；
或在运行时设 `MIAIR_STREAM_MODE=wav` 退回最关键的流行为。

## Docker 部署（继承上游，Thanks @SyunSS）

支持平台：Linux / OpenWrt / macOS

```bash
# 安装 Git（OpenWrt）
opkg update && opkg install git git-http

# 克隆并部署
git clone https://github.com/Kerwin75631591/MiAir.git
cd MiAir
chmod +x deploy.sh manage.sh
./deploy.sh
```

安装完成后访问 `http://容器宿主机IP:8300`。
请确保容器网络为 **Host**、重启策略为 **Unless Stopped**；部分情况下修改配置后容器可能不会自动重启，请手动重启。

```bash
docker logs -f miair     # 查看日志
docker stop miair        # 停止
docker start miair       # 启动
docker restart miair     # 重启
```

## 我们

上游交流群（[需要帮助&交流&测试版本发布](https://qun.qq.com/universal-share/share?ac=1&authKey=1zXhx2zxgw9GG2mkecypT9clD7q0B3W3l4K0D4fQirmpDWakz0Oy2BI3ocDrgzbh&busi_data=eyJncm91cENvZGUiOiI3NDEyNjcyOTgiLCJ0b2tlbiI6InYwbitXQTF5cE9MaUJCR0hMUk03OWV0WkFoMThxbjJRaWI4dHVlbUpGdW5OdEZBVEpXMXF0T1dQUnRmRXRzYVgiLCJ1aW4iOiIxODQxOTM4MDQwIn0%3D&data=_OrA-eASJMwYwx-Uj-BReC1Xh3zGAdkn8CQskbEsQ5S66bhqvvO6dJ-QrSlRl-Ks00l5XDw1FANE8Um0w5yB8Q&svctype=4&tempid=h5_group_info "需要帮助&交流&测试版本发布")）

## 许可证说明

上游仓库**未提供 LICENSE 文件**，但代码中混合了多种许可证来源（如 `airplay/fairplay3.py`、
`airplay/dxxp.py` 标注 GPLv2，`airplay/airplay1.py` 为 LGPL-2.0+，另有参照 MaCast（GPL-3.0）的实现）。
本 fork 原样继承该状态，**分发或商用前请自行厘清许可证问题**；个人自用无碍。

---

[![preview](preview.png "preview")](preview.png "preview")
