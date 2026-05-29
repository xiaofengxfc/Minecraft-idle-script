# Minecraft AFK 挂机互保脚本

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

在同一台电脑上同时运行两个 Minecraft 客户端时，通过本地 TCP 互联实现双实例互保：一方掉线（进程退出/崩溃/网络断开），另一方自动结束自己的 MC 客户端，避免单角色在线被怪物杀死或物资丢失。

---

## 目录

- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [安装](#安装)
- [运行方式](#运行方式)
- [命令行参数](#命令行参数)
- [配置文件](#配置文件)
- [工作原理](#工作原理)
- [协议规范](#协议规范)
- [使用场景](#使用场景)
- [项目结构](#项目结构)
- [测试](#测试)
- [常见问题](#常见问题)
- [更新日志](#更新日志)

---

## 核心特性

- **双向 TCP 心跳** + 告别消息协议（`PEER_DOWN` / `SHUTDOWN` / `ALIVE`）
- **服务器连接断开检测**：MC 掉线但进程未退出时也能触发保护（多级检测 + netstat 回退）
- **多级进程终止**：`terminate` → `kill` → `taskkill` 兜底
- **掉线后自动重启 Minecraft**（需配置 `--restart-command`）
- **启动宽限期**（默认 90s）：允许两个实例先后启动，期间不触发超时判定
- **指数退避重连**：2s → 4s → 8s → ... → 30s
- **TCP Keep-Alive** + **TCP NODELAY** 优化
- **Webhook 通知**支持（Discord / 企业微信 / 飞书等）
- **配置文件集中管理**（`config.json`）
- **日志输出**：控制台 + 文件（可选）
- **宽泛的 MC 进程识别**：支持原版 / Forge / Fabric / 各类启动器（HMCL、PCL、BakaXL 等）
- **psutil 自动安装** + netstat 回退检测

---

## 快速开始

1. **安装依赖**

   ```bash
   pip install -r requirements.txt
   ```

   （或直接运行脚本，会自动安装 `psutil`）

2. **在同一台电脑上启动两个 Minecraft 客户端**（需进入游戏世界）

3. **双击运行** `start_instance_a.bat`（对应第一个 MC 窗口）

4. **双击运行** `start_instance_b.bat`（对应第二个 MC 窗口）

5. 两个 CMD 窗口保持开启即可挂机

6. 按 `Ctrl+C` 可安全退出（会发送 `SHUTDOWN` 通知对方）

---

## 安装

### 环境要求

- **操作系统**：Windows 10/11、Linux、macOS
- **Python**：3.8 及以上版本
- **Minecraft**：Java 版（任意版本/启动器）

### 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/xiaofengxfc/Minecraft-idle-script.git
cd Minecraft-idle-script

# 2. 安装依赖
pip install -r requirements.txt

# 3. （可选）编辑 config.json 自定义配置
# 4. 启动两个 Minecraft 客户端后运行脚本
```

---

## 运行方式

### 方式一：批处理文件（推荐）

| 文件 | 说明 |
|------|------|
| `start_instance_a.bat` | 实例 A（端口 18888，对端 18889，绑定第一个 MC 进程） |
| `start_instance_b.bat` | 实例 B（端口 18889，对端 18888，绑定第二个 MC 进程） |

### 方式二：`--instance` 快捷参数（自动读取 config.json）

```bash
python afk_monitor.py --instance a
python afk_monitor.py --instance b
```

### 方式三：全自动模式（自动检测 MC 进程）

```bash
# 实例 A
python afk_monitor.py --port 18888 --peer-port 18889 --auto --auto-index 0

# 实例 B
python afk_monitor.py --port 18889 --peer-port 18888 --auto --auto-index 1
```

### 方式四：手动指定 PID

```bash
python afk_monitor.py --port 18888 --peer-port 18889 --pid <MC进程PID>
```

### 查看所有 Minecraft 进程

```bash
python afk_monitor.py --list
```

输出示例：

```
检测到 2 个 Minecraft 进程:
============================================================
  PID: 12345    | java.exe [Minecraft]
  命令行: java -Xmx2G -Dminecraft.client.jar ...

  PID: 12346    | java.exe [Minecraft]
  命令行: java -Xmx2G -Dminecraft.client.jar ...
```

---

## 命令行参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--port` | int | 18888 | 本实例监听端口号 |
| `--peer-port` | int | 18889 | 对方实例监听端口号 |
| `--pid` | int | — | 手动指定要监控的 MC 进程 PID |
| `--auto` | flag | — | 全自动检测 MC 进程（需配合 `--auto-index`） |
| `--auto-index` | int | 0 | 自动模式下选择第几个进程（0 = 第一个，按 PID 升序） |
| `--instance` | str | — | 使用预设实例快捷配置（`a` 或 `b`） |
| `--config` | str | `config.json` | 配置文件路径 |
| `--list` | flag | — | 列出所有 Minecraft 进程后退出 |
| `--heartbeat-interval` | int | 3 | 心跳间隔（秒），覆盖配置文件 |
| `--heartbeat-timeout` | int | 15 | 心跳超时（秒），覆盖配置文件 |
| `--server-check-interval` | int | 10 | 服务器连接检测间隔（秒），覆盖配置文件 |
| `--no-check-server` | flag | — | 禁用服务器连接断开检测 |
| `--log-file` | str | — | 日志文件路径，覆盖配置文件 |
| `--webhook-url` | str | — | Webhook 通知地址，覆盖配置文件 |
| `--restart-command` | str | — | 掉线后自动重启 Minecraft 的完整命令行 |

---

## 配置文件

配置通过 `config.json` 集中管理，命令行参数可覆盖配置文件中的值。

### 完整配置示例

```json
{
    "heartbeat_interval": 3,
    "heartbeat_timeout": 15,
    "reconnect_interval_min": 2,
    "reconnect_interval_max": 30,
    "startup_grace_period": 90,
    "tcp_keepalive_idle": 10,
    "tcp_keepalive_interval": 5,
    "tcp_keepalive_count": 3,
    "server_check_interval": 10,
    "server_check_consecutive": 2,
    "recv_buffer_max": 65536,
    "instance_a": {
        "port": 18888,
        "peer_port": 18889,
        "auto_index": 0,
        "title": "实例A"
    },
    "instance_b": {
        "port": 18889,
        "peer_port": 18888,
        "auto_index": 1,
        "title": "实例B"
    },
    "log_file": "afk_monitor.log",
    "webhook_url": ""
}
```

### 配置项说明

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `heartbeat_interval` | int | 3 | 心跳发送间隔（秒） |
| `heartbeat_timeout` | int | 15 | 心跳超时判定（秒），超时未收到心跳则判定对方掉线 |
| `reconnect_interval_min` | int | 2 | 重连起始等待间隔（秒） |
| `reconnect_interval_max` | int | 30 | 重连最大等待间隔（秒） |
| `startup_grace_period` | int | 90 | 启动宽限期（秒），允许两个实例先后启动 |
| `tcp_keepalive_idle` | int | 10 | TCP Keep-Alive 空闲时间（秒） |
| `tcp_keepalive_interval` | int | 5 | TCP Keep-Alive 探测间隔（秒） |
| `tcp_keepalive_count` | int | 3 | TCP Keep-Alive 探测次数 |
| `server_check_interval` | int | 10 | 服务器连接检测间隔（秒） |
| `server_check_consecutive` | int | 2 | 连续检测 N 次断开才触发掉线 |
| `recv_buffer_max` | int | 65536 | 接收缓冲区大小上限（防止内存溢出） |
| `instance_a` | object | — | 实例 A 预设配置 |
| `instance_b` | object | — | 实例 B 预设配置 |
| `log_file` | string | — | 日志文件路径（留空则不写文件） |
| `webhook_url` | string | — | Webhook 通知地址（支持 Discord/企业微信/飞书格式） |

> **提示**：如需本地覆盖配置而不提交到 Git，可创建 `config.local.json`，使用 `--config config.local.json` 启动。该文件已在 `.gitignore` 中排除。

---

## 工作原理

### 架构概览

```
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│        实例 A (afk_monitor)      │     │        实例 B (afk_monitor)      │
│                                 │     │                                 │
│  监听端口: 18888                 │ TCP │  监听端口: 18889                 │
│  对端端口: 18889                 │◄───►│  对端端口: 18888                 │
│                                 │     │                                 │
│  监控 MC 进程 PID: X             │     │  监控 MC 进程 PID: Y             │
└─────────────────────────────────┘     └─────────────────────────────────┘
          │                                        │
          ▼                                        ▼
   ┌──────────────┐                        ┌──────────────┐
   │ Minecraft A   │                        │ Minecraft B   │
   │  (PID: X)     │                        │  (PID: Y)     │
   └──────────────┘                        └──────────────┘
```

### 1. TCP 心跳机制

两个脚本实例在 `127.0.0.1` 上通过指定端口建立 TCP 连接，按设定间隔交换 `ALIVE` 心跳包。当一方超过 `heartbeat_timeout` 秒未收到心跳时，判定对方掉线。

### 2. 告别消息协议

三种消息类型确保双方在异常和正常退出时都能正确响应：

| 消息 | 含义 | 触发场景 |
|------|------|----------|
| `ALIVE` | 心跳包 | 定时发送 |
| `PEER_DOWN` | 本地 MC 进程退出 | 监控到本地 MC 进程已退出 |
| `SHUTDOWN` | 脚本正常退出 | 用户按 `Ctrl+C` 手动退出 |

### 3. 服务器连接检测

定期检测 MC 进程的远程 TCP 连接状态：
- 通过 `psutil` 获取进程的网络连接
- 排除本地回环（`127.0.0.1`、`::1`）和非游戏端口（HTTP、DNS、SMTP 等）
- 当 `psutil` 权限不足时自动回退到 `netstat` 命令
- 连续 N 次未检测到游戏服务器连接 → 判定掉线

### 4. 掉线处理流程

```
一方掉线 (MC进程退出/崩溃/断网)
  │
  ├──► 发送 PEER_DOWN 消息给对方（如 TCP 连接仍存在）
  │
  └──► 对方实例收到 PEER_DOWN 或心跳超时
        │
        ├──► 终止本地 MC 进程 (terminate → kill → taskkill)
        │
        ├──► 发送 Webhook 通知（如已配置）
        │
        └──► 自动重启 Minecraft（如已配置 --restart-command）
```

### 5. 安全退出（Ctrl+C）

```
用户按下 Ctrl+C
  │
  ├──► 发送 SHUTDOWN 消息给对方
  │
  └──► 对方收到 SHUTDOWN
        │
        └──► 终止本地 MC 进程
```

---

## 协议规范

### TCP 通信协议

- **传输层**：TCP over IPv4 (`127.0.0.1`)
- **消息格式**：以换行符 `\n` 分隔的纯文本消息
- **消息类型**：

| 消息内容 | 字节表示 | 方向 | 说明 |
|----------|----------|------|------|
| `ALIVE` | `b"ALIVE\n"` | 双向 | 心跳保活 |
| `PEER_DOWN` | `b"PEER_DOWN\n"` | 单向 | 通知对方本地 MC 进程已退出 |
| `SHUTDOWN` | `b"SHUTDOWN\n"` | 单向 | 通知对方脚本正常退出 |

### 接收缓冲区解析

接收端使用循环解析算法处理粘包/半包问题：识别缓冲区中所有已知消息类型，从前往后逐条提取并处理，残留数据保留至下次接收。

### 连接管理

- **先到先得**：当同时存在 Server 侧和 Client 侧两条连接时，只有第一条被接纳为活跃连接
- **TCP 优化**：启用 `TCP_NODELAY`（禁用 Nagle 算法）和 `SO_KEEPALIVE`
- **重连退避**：连接断开后按指数退避重试（2s → 4s → 8s → ... → 30s）

---

## 使用场景

### 场景 1：普通挂机

1. 打开两个启动器（HMCL / PCL / BakaXL），启动两个 MC 客户端
2. 运行 `start_instance_a.bat` → 窗口 1 监控 PID X
3. 运行 `start_instance_b.bat` → 窗口 2 监控 PID Y
4. 两个 CMD 窗口保持开启即可挂机
5. 一方崩溃 → 另一方自动关闭 → 两个 MC 都退出

### 场景 2：排位/副本互保

1. 同上启动两个 MC 客户端并进入同一服务器
2. 分别启动脚本
3. 如一人掉线，另一人 MC 也会退出 → 避免单人在线暴露风险

### 场景 3：服务器维护/重启

1. 正常挂机中
2. 服务器踢出所有玩家
3. 脚本检测到服务器连接断开 → 结束本地 MC 进程 → 通知对方

### 场景 4：自动重启

```bash
python afk_monitor.py --instance a --restart-command "start mc_a.bat"
```

掉线后自动重新启动 Minecraft，配合启动器实现无人值守挂机。

---

## 项目结构

```
挂机脚本/
├── afk_monitor.py          # 核心脚本（主程序）
├── error_logger.py         # 全局报错日志模块
├── config.json             # 集中配置文件
├── start_instance_a.bat    # 实例 A 启动脚本（Windows）
├── start_instance_b.bat    # 实例 B 启动脚本（Windows）
├── requirements.txt        # Python 依赖列表
├── test_afk_monitor.py     # 单元测试
├── logs/                   # 报错日志输出目录（自动创建）
├── .gitignore              # Git 忽略规则
├── 使用说明.txt            # 中文使用说明（纯文本）
└── README.md               # 本文件
```

### 文件说明

| 文件 | 说明 |
|------|------|
| `afk_monitor.py` | 主程序，包含 TCP 心跳、进程监控、服务器连接检测、配置管理等全部功能 |
| `error_logger.py` | 全局报错日志模块，捕获未处理异常并记录进程/线程快照 |
| `config.json` | 集中配置文件，管理心跳参数、端口、实例预设、Webhook 等 |
| `start_instance_a.bat` | 实例 A 一键启动脚本，自动使用 `--instance a --auto` |
| `start_instance_b.bat` | 实例 B 一键启动脚本，自动使用 `--instance b --auto` |
| `requirements.txt` | Python 依赖（`psutil>=5.9.0`） |
| `test_afk_monitor.py` | 单元测试和集成测试 |
| `logs/` | 报错日志输出目录（自动创建，保留最近 20 个文件） |
| `.gitignore` | 排除 `__pycache__`、`.pyc`、`*.log`、`logs/`、`config.local.json` 等 |

### 核心类与模块

| 类/模块 | 职责 |
|---------|------|
| `ProtocolMessage` | 心跳协议消息常量定义 |
| `AppConfig` | 应用配置管理（从命令行参数和配置文件加载） |
| `PeerConnection` | 对等连接管理器（TCP 服务端/客户端、双向心跳、掉线判定） |
| `ServerConnectionMonitor` | 服务器连接监控（psutil + netstat 回退） |
| `MonitorApp` | 主应用程序（流程编排、信号处理、回调注册） |

---

## 全局报错日志

`error_logger.py` 提供一套完整的全局异常捕获和诊断日志系统，在脚本发生任何未处理异常时自动记录详细信息，便于排查问题。

### 核心功能

- **全局异常捕获**：通过 `sys.excepthook` 捕获主线程未处理异常，通过 `threading.excepthook`（Python 3.8+）捕获子线程未处理异常
- **自动日志文件管理**：在 `logs/` 目录下创建带时间戳的报错日志文件（如 `error_20250101_120000.log`），自动清理保留最近 20 个
- **进程/线程快照**：异常发生时自动记录当前进程 CPU/内存使用、活跃线程列表、系统内存状态
- **与 afk_monitor 日志集成**：`afk_monitor.py` 中的 `log.error()` 等调用会同步写入报错日志文件
- **优雅关闭**：脚本退出时自动恢复原始异常钩子，写入关闭标记

### 日志写入方式

| 写入方式 | 说明 |
|----------|------|
| 全局异常捕获 | 任何未被 `try/except` 捕获的异常自动写入 |
| `write_error_to_log()` | 手动向报错日志写入一条消息（用于被捕获但仍需记录的致命错误） |
| logging 集成 | `logging.getLogger("afk_monitor")` 的 FileHandler 自动同步 |

### 日志文件格式

```
============================================================
[2025-01-01 12:00:00] [INFO] 报错日志系统已初始化
  Python 版本: 3.12.0
  平台: win32
  工作目录: D:\Minecraft-idle-script
  脚本目录: D:\Minecraft-idle-script
============================================================

...（运行日志）...

============================================================
[2025-01-01 12:30:15] [CRITICAL] 未捕获的异常
============================================================
Traceback (most recent call last):
  ...
============================================================

[2025-01-01 12:30:15] [DEBUG] 系统快照:
  当前进程 PID: 12345, 名称: python.exe
  CPU 使用率: 2.3%
  内存使用: RSS=45.2MB, VMS=120.5MB
  活跃线程数: 8
    - MainThread (Alive, NonDaemon)
    - ServerThread (Alive, Daemon)
    ...
  系统内存: 总量=16.0GB, 可用=8.2GB, 使用率=48.8%
```

---

## 测试

项目包含完整的单元测试和集成测试，使用 Python 标准库 `unittest` 框架。

### 运行测试

```bash
# 使用 pytest（推荐）
pytest test_afk_monitor.py -v

# 或使用 unittest
python -m pytest test_afk_monitor.py -v
```

### 测试覆盖

| 测试类 | 覆盖内容 |
|--------|----------|
| `TestProtocolMessage` | 协议消息常量格式验证 |
| `TestIsLikelyGamePort` | 游戏端口判定逻辑（端口过滤规则） |
| `TestCheckProcessAlive` | 进程存活检测 |
| `TestSafeCloseSocket` | Socket 安全关闭（正常/已关闭/None） |
| `TestConfigManagement` | 配置加载与读写 |
| `TestFindMinecraftProcesses` | MC 进程检测与 PID 排序 |
| `TestPeerConnectionHeartbeat` | 端到端心跳通信、掉线回调防重入、线程启动 |
| `TestRecvBufferProtection` | 接收缓冲区溢出防护 |
| `TestLoggingSetup` | 日志配置（控制台 + 文件） |
| `TestNetstatFallbackParsing` | netstat 输出解析（IPv4、本地回环过滤） |
| `TestIntegrationQuick` | 模块导入完整性、config.json 可解析性验证 |

---

## 常见问题

### Q: 脚本提示"未检测到 Minecraft 进程"

**A:** 确保 MC 客户端已完全启动（进入游戏世界），使用 `python afk_monitor.py --list` 查看当前所有 MC 进程。

### Q: PID 绑定错误（实例 A 绑了实例 B 的 MC）

**A:** 使用 `--list` 确认 PID 排序，必要时用 `--pid` 手动指定。两个 MC 启动顺序会影响 PID 排列（先启动的通常 PID 更小）。

### Q: `psutil.AccessDenied` 权限不足

**A:** 右键 CMD → 以管理员身份运行。脚本会自动回退到 `netstat` 备用方案，但服务器连接检测部分功能可能受限。

### Q: 防火墙阻止本地 TCP 连接

**A:** 本地回环 `127.0.0.1` 通常不会被防火墙拦截。如遇到问题，请在防火墙中放行 `python.exe` 或相应端口（18888/18889）。

### Q: 如何自定义端口？

**A:** 编辑 `config.json` 中的 `instance_a.port` / `instance_b.port`，或使用命令行参数：

```bash
python afk_monitor.py --port 20000 --peer-port 20001 --auto --auto-index 0
```

### Q: 如何开启 Webhook 通知？

**A:** 在 `config.json` 中设置 `"webhook_url"` 为你的 Webhook 地址：

```json
{
    "webhook_url": "https://discord.com/api/webhooks/xxx/yyy"
}
```

支持 Discord、企业微信、飞书等兼容 `{"content": "message"}` JSON 格式的 Webhook。

### Q: 如何实现掉线后自动重启 Minecraft？

**A:** 使用 `--restart-command` 参数：

```bash
python afk_monitor.py --instance a --restart-command "start mc_a.bat"
```

### Q: 可以在不同电脑上运行吗？

**A:** 当前版本设计为同一台电脑上的本地 TCP 互联（`127.0.0.1`）。如需跨机器互保，需修改代码中的 `LOCALHOST` 常量和端口绑定逻辑。

### Q: 服务器连接检测显示"未检测到游戏服务器连接"但游戏中正常

**A:** 可能是 `psutil` 权限不足或服务器使用了非标准端口。脚本会自动使用 `netstat` 回退方案。如果持续误报，可使用 `--no-check-server` 禁用此功能。

---

## 更新日志

### v2.0 — 全面重构

- ✨ 告别消息协议（`PEER_DOWN` / `SHUTDOWN`），替代心跳计数判定
- ✨ 服务器连接断开检测（多级检测 + netstat 回退）
- ✨ 多级进程终止（`terminate` → `kill` → `taskkill`）
- ✨ 掉线后自动重启功能
- ✨ 启动宽限期 + 指数退避重连
- ✨ TCP Keep-Alive + TCP NODELAY 优化
- ✨ Webhook 通知支持
- ✨ `config.json` 集中配置管理
- ✨ logging 日志系统（控制台 + 文件）
- ✨ psutil 自动安装
- ✨ 宽泛 MC 进程识别
- ✨ 接收缓冲区溢出防护
- ✨ 全局报错日志系统（`error_logger.py`）：未处理异常自动捕获、进程/线程快照、自动清理
- ✨ 完整单元测试覆盖

---

## 许可证

MIT License

---

## 相关链接

- [GitHub 仓库](https://github.com/xiaofengxfc/Minecraft-idle-script)
- [psutil 文档](https://psutil.readthedocs.io/)
- [Python Socket 编程](https://docs.python.org/3/library/socket.html)