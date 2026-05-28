#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft AFK 挂机互保脚本
============================
功能：两个 Minecraft 客户端在同一台机器上运行时，通过本地 TCP 互联互相检测在线状态。
      一方掉线（进程退出或网络断开），另一方自动结束自己的 Minecraft 客户端进程。

使用方式：
    实例A: python afk_monitor.py --instance a
    实例B: python afk_monitor.py --instance b
    手动:  python afk_monitor.py --port 18888 --peer-port 18889 --pid <MC进程PID>

依赖：psutil（脚本会自动尝试安装）
"""

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ==================== 日志配置 ====================
LOG_FORMAT = '[%(asctime)s] [%(levelname)s] %(message)s'
LOG_DATE_FORMAT = '%H:%M:%S'

log = logging.getLogger("afk_monitor")


def setup_logging(log_file: Optional[str] = None, level: int = logging.INFO):
    """配置控制台和文件日志输出"""
    logger = logging.getLogger("afk_monitor")
    logger.setLevel(level)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logger.addHandler(console_handler)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning(f"无法创建日志文件 {log_file}: {e}")


# ==================== 自动安装依赖 ====================
def ensure_psutil():
    """确保 psutil 已安装，否则自动安装"""
    try:
        import psutil  # noqa: F811
        return psutil
    except ImportError:
        log.warning("psutil 未安装，正在自动安装...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "psutil"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log.info("psutil 安装成功")
            import psutil  # noqa: F811
            return psutil
        except Exception as e:
            log.error(f"psutil 安装失败: {e}")
            log.error("请手动执行: pip install psutil")
            sys.exit(1)


psutil = ensure_psutil()

LOCALHOST = "127.0.0.1"


# ==================== 协议消息定义 ====================
@dataclass
class ProtocolMessage:
    """心跳协议消息常量"""
    HEARTBEAT: bytes = b"ALIVE\n"
    PEER_DOWN: bytes = b"PEER_DOWN\n"
    SHUTDOWN: bytes = b"SHUTDOWN\n"

    @classmethod
    def all_messages(cls) -> List[bytes]:
        return [cls.HEARTBEAT, cls.PEER_DOWN, cls.SHUTDOWN]


Protocol = ProtocolMessage()


# ==================== 配置管理 ====================
@dataclass
class AppConfig:
    """应用程序运行配置"""
    port: int = 18888
    peer_port: int = 18889
    auto_index: int = 0
    instance_title: str = ""

    heartbeat_interval: int = 3
    heartbeat_timeout: int = 15
    reconnect_interval_min: int = 2
    reconnect_interval_max: int = 30
    startup_grace_period: int = 90
    tcp_keepalive_idle: int = 10
    tcp_keepalive_interval: int = 5
    tcp_keepalive_count: int = 3
    server_check_interval: int = 10
    server_check_consecutive: int = 2
    recv_buffer_max: int = 65536

    log_file: str = ""
    webhook_url: str = ""
    restart_command: str = ""
    no_check_server: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "AppConfig":
        """从命令行参数创建配置，支持 config.json 文件预设"""
        config = cls()
        config.no_check_server = args.no_check_server
        config.restart_command = args.restart_command or ""

        # 尝试从配置文件加载默认值
        config_file = Path(args.config) if args.config else Path("config.json")
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                config._load_from_dict(data)
                log.info(f"已从 {config_file} 加载配置")
            except Exception as e:
                log.warning(f"加载配置文件失败: {e}")

        # 命令行参数覆盖配置文件（-1 表示未指定）
        if args.heartbeat_interval >= 0:
            config.heartbeat_interval = args.heartbeat_interval
        if args.heartbeat_timeout >= 0:
            config.heartbeat_timeout = args.heartbeat_timeout
        if args.server_check_interval >= 0:
            config.server_check_interval = args.server_check_interval
        if args.webhook_url:
            config.webhook_url = args.webhook_url
        if args.log_file:
            config.log_file = args.log_file

        # 实例快捷参数
        if args.instance:
            instances = data.get("instance_a", {}), data.get("instance_b", {})
            if args.instance == 'a':
                inst = instances[0] if instances[0] else {"port": 18888, "peer_port": 18889, "auto_index": 0}
            else:
                inst = instances[1] if instances[1] else {"port": 18889, "peer_port": 18888, "auto_index": 1}
            config.port = args.port if args.port is not None else inst.get("port", config.port)
            config.peer_port = args.peer_port if args.peer_port is not None else inst.get("peer_port", config.peer_port)
            config.auto_index = inst.get("auto_index", config.auto_index)
            config.instance_title = inst.get("title", f"实例{args.instance.upper()}")
        else:
            config.port = args.port if args.port is not None else config.port
            config.peer_port = args.peer_port if args.peer_port is not None else config.peer_port
            config.auto_index = args.auto_index

        return config

    def _load_from_dict(self, data: dict):
        """从字典加载配置值"""
        for key in ("heartbeat_interval", "heartbeat_timeout", "reconnect_interval_min",
                     "reconnect_interval_max", "startup_grace_period",
                     "tcp_keepalive_idle", "tcp_keepalive_interval", "tcp_keepalive_count",
                     "server_check_interval", "server_check_consecutive", "recv_buffer_max"):
            if key in data:
                setattr(self, key, data[key])
        for key in ("log_file", "webhook_url"):
            if key in data and not getattr(self, key):
                setattr(self, key, data[key])


# ==================== Minecraft 进程自动检测 ====================
def find_minecraft_processes() -> List[Tuple[int, str, str]]:
    """
    扫描系统中所有正在运行的 Minecraft Java 进程。
    返回按 PID 升序排列的 (pid, process_name, command_line) 列表。
    """
    minecraft_processes: List[Tuple[int, str, str]] = []

    mc_keywords = [
        'minecraft', 'forge', 'fabric', 'nide8auth', 'authlib-injector',
        'launcher', 'LaunchClient', '-Dminecraft', 'lwjgl',
        'tlauncher', 'hmcl', 'pcl', 'bakaxl', 'plaincraft',
        'launchwrapper'
    ]

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            info = proc.info
            name = (info.get('name') or '').lower()
            pid = info.get('pid')
            cmdline = ' '.join(info.get('cmdline') or [])
            cmdline_lower = cmdline.lower()

            is_java = 'java' in name or 'javaw' in name or 'minecraft' in name
            if not is_java:
                continue

            is_mc = any(kw.lower() in cmdline_lower for kw in mc_keywords)
            if not is_mc:
                continue

            if 'minecraft' in cmdline_lower:
                display = f"{name} [Minecraft]"
            elif any(k in cmdline_lower for k in ['forge', 'fabric']):
                display = f"{name} [Modded MC]"
            else:
                display = f"{name} [MC Launcher]"

            minecraft_processes.append((pid, display, cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    minecraft_processes.sort(key=lambda p: p[0])
    return minecraft_processes


# ==================== 服务器连接检测 ====================
# 非游戏服务器端口（认证/API/CDN/Web）
_NON_GAME_PORTS: set = {
    80, 443, 8080, 8443,
    21, 22, 23,
    53,
    110, 143, 993, 995,
    25, 465, 587,
}

_MC_DEFAULT_PORT = 25565


def is_likely_game_port(port: int) -> bool:
    """判断端口是否可能是 Minecraft 游戏服务器端口"""
    if port in _NON_GAME_PORTS:
        return False
    if port == _MC_DEFAULT_PORT:
        return True
    if port >= 1024:
        return True
    return False


def get_minecraft_server_connection(pid: int) -> Optional[Tuple[str, int]]:
    """
    获取 Minecraft 进程的主游戏服务器连接（排除本地回环和非游戏端口）。
    """
    try:
        for conn in psutil.Process(pid).net_connections(kind='tcp'):
            if conn.status != 'ESTABLISHED' or not conn.raddr:
                continue
            ip = conn.raddr.ip
            if ip.startswith('127.') or ip in ('::1', '0.0.0.0'):
                continue
            if is_likely_game_port(conn.raddr.port):
                return (ip, conn.raddr.port)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return None


def get_all_server_connections(pid: int) -> List[Tuple[str, int]]:
    """获取所有远程连接（排除本地回环）"""
    result: List[Tuple[str, int]] = []
    try:
        for conn in psutil.Process(pid).net_connections(kind='tcp'):
            if conn.status != 'ESTABLISHED' or not conn.raddr:
                continue
            ip = conn.raddr.ip
            if ip.startswith('127.') or ip in ('::1', '0.0.0.0'):
                continue
            result.append((ip, conn.raddr.port))
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return result


def get_server_connections_fallback(pid: int) -> List[Tuple[str, int]]:
    """
    备用方案：通过 netstat 命令获取 TCP 连接（当 psutil 权限不足时使用）。
    """
    result: List[Tuple[str, int]] = []
    try:
        output = subprocess.check_output(
            ["netstat", "-ano"], timeout=5,
            stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='replace')
    except Exception:
        return result

    pid_str = str(pid)
    for line in output.splitlines():
        line = line.strip()
        if not line or not line.endswith(pid_str):
            continue
        if 'ESTABLISHED' not in line.upper():
            continue

        parts = line.split()
        if len(parts) < 5:
            continue

        foreign = parts[2]
        if ':' not in foreign:
            continue

        try:
            ip_port = str(foreign)
            if ip_port.startswith('['):
                bracket_end = ip_port.rfind(']')
                if bracket_end > 0 and ':' in ip_port[bracket_end:]:
                    ip = ip_port[:bracket_end + 1]
                    port_str = ip_port[bracket_end + 2:]
                else:
                    continue
            else:
                last_colon = ip_port.rfind(':')
                if last_colon <= 0:
                    continue
                ip = ip_port[:last_colon]
                port_str = ip_port[last_colon + 1:]

            port = int(port_str)
            if ip in ('127.0.0.1', '::1', '0.0.0.0', '[::1]'):
                continue
            if ip.startswith('127.'):
                continue
            result.append((ip, port))
        except (ValueError, IndexError):
            continue
    return result


# ==================== 进程管理 ====================
def check_process_alive(pid: int) -> bool:
    """检查指定 PID 进程是否存活"""
    try:
        return psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process(pid: int) -> bool:
    """
    终止指定 PID 进程。
    先尝试 terminate()，3 秒无响应则 kill()，再失败则用 taskkill 兜底。
    """
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        log.warning(f"正在终止进程: {name} (PID: {pid})")
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            log.warning(f"进程未响应 terminate，强制 kill: PID {pid}")
            proc.kill()
        log.info(f"进程已终止: PID {pid}")
        return True
    except psutil.NoSuchProcess:
        log.info(f"进程已不存在: PID {pid}")
        return True
    except Exception as e:
        log.error(f"终止进程失败 (PID {pid}): {e}")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5)
            log.info(f"通过 taskkill 终止: PID {pid}")
            return True
        except Exception as e2:
            log.error(f"taskkill 失败: {e2}")
            return False


def restart_minecraft(command: str) -> Optional[subprocess.Popen]:
    """执行 Minecraft 重启命令，返回启动的进程对象"""
    if not command:
        return None
    log.info(f"正在重启 Minecraft: {command}")
    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        log.info(f"Minecraft 重启中 (PID: {proc.pid})")
        return proc
    except Exception as e:
        log.error(f"Minecraft 重启失败: {e}")
        return None


# ==================== Webhook 通知 ====================
def send_webhook(url: str, message: str) -> None:
    """发送 Webhook 通知（支持 Discord/企业微信等格式）"""
    if not url:
        return
    try:
        import urllib.request
        data = json.dumps({"content": message}).encode('utf-8')
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        log.info("Webhook 通知已发送")
    except Exception as e:
        log.warning(f"Webhook 发送失败: {e}")


# ==================== TCP 连接工具 ====================
def optimize_tcp_socket(sock: socket.socket, config: AppConfig) -> None:
    """配置 TCP socket 优化选项"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    try:
        if sys.platform != 'win32':
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, config.tcp_keepalive_idle)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, config.tcp_keepalive_interval)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, config.tcp_keepalive_count)
        else:
            SIO_KEEPALIVE_VALS = 0x98000004
            sock.ioctl(
                SIO_KEEPALIVE_VALS,
                (1, config.tcp_keepalive_idle * 1000, config.tcp_keepalive_interval * 1000)
            )
    except (OSError, AttributeError, ImportError):
        pass


def safe_close_socket(sock: Optional[socket.socket]) -> None:
    """安全关闭 socket"""
    if sock:
        try:
            sock.close()
        except Exception:
            pass


# ==================== 对等连接管理器 ====================
class PeerConnection:
    """
    管理两个脚本实例间的 TCP 心跳连接。

    判定对方退出的三种路径（满足任一即触发）：
    1. 收到 PEER_DOWN 或 SHUTDOWN 消息 → 立即判定
    2. 心跳超时（连续 heartbeat_timeout 秒未收到 ALIVE）→ 兜底判定
    3. 服务器连接断开检测（可选）
    """

    def __init__(self, config: AppConfig, monitored_pid: int,
                 on_peer_lost: Callable[[], None]):
        self.config = config
        self.monitored_pid = monitored_pid
        self.on_peer_lost = on_peer_lost

        self.server_socket: Optional[socket.socket] = None
        self.last_heartbeat: float = time.time()
        self.startup_time: float = time.time()
        self.peer_ever_connected: bool = False
        self.running: bool = True

        self._conn_lock = threading.Lock()
        self._active_socket: Optional[socket.socket] = None
        self._peer_lost_triggered: bool = False
        self._client_reconnect_backoff: int = config.reconnect_interval_min
        self._shutting_down: bool = False
        self._psutil_permission_warned: bool = False  # 权限警告去重标记

        # 线程
        self.server_thread = threading.Thread(
            target=self._run_server, daemon=True, name="ServerThread")
        self.client_thread = threading.Thread(
            target=self._run_client, daemon=True, name="ClientThread")
        self.checker_thread = threading.Thread(
            target=self._heartbeat_checker, daemon=True, name="CheckerThread")
        self.proc_monitor_thread = threading.Thread(
            target=self._local_process_monitor, daemon=True, name="ProcMonitorThread")

    # ---------- 活跃连接管理 ----------
    def _try_claim_active_socket(self, sock: socket.socket) -> bool:
        """尝试将 socket 设为活跃连接，先到先得"""
        with self._conn_lock:
            if self._active_socket is None and not self._peer_lost_triggered:
                self._active_socket = sock
                return True
            return False

    def _clear_active_socket(self, sock: Optional[socket.socket] = None) -> None:
        """清除活跃连接"""
        with self._conn_lock:
            if sock is None or self._active_socket is sock:
                self._active_socket = None

    # ---------- 告别消息发送 ----------
    def _send_farewell(self, msg: bytes) -> None:
        """通过活跃 socket 发送告别消息，最多重试 3 次"""
        sock: Optional[socket.socket] = None
        with self._conn_lock:
            sock = self._active_socket
        if sock is None:
            log.debug("无活跃连接，跳过发送告别消息")
            return
        try:
            sock.settimeout(2)
        except OSError:
            pass
        for attempt in range(3):
            try:
                sock.sendall(msg)
                log.info(f"已向对方发送告别消息: {msg.decode().strip()}")
                return
            except OSError as e:
                if attempt < 2:
                    time.sleep(0.5)
                else:
                    log.warning(f"发送告别消息失败: {e}")

    # ---------- 判定对方掉线 ----------
    def _trigger_peer_lost(self, reason: str = "") -> None:
        """线程安全地触发 peer lost 回调，确保只执行一次"""
        with self._conn_lock:
            if self._peer_lost_triggered:
                return
            self._peer_lost_triggered = True
        if reason:
            log.warning(f"检测到对方掉线: {reason}")
        log.warning("对方已断开连接，正在结束本地 Minecraft 客户端...")
        self.running = False
        self.on_peer_lost()

    # ---------- 双向心跳 ----------
    def _start_bidirectional_heartbeat(self, sock: socket.socket,
                                       role_name: str) -> bool:
        """在已建立的 TCP 连接上启动双向心跳"""
        if not self._try_claim_active_socket(sock):
            log.debug(f"[{role_name}] 连接未被接纳（已有活跃连接），关闭")
            return False

        optimize_tcp_socket(sock, self.config)
        try:
            sock.settimeout(self.config.heartbeat_interval + 2)
        except OSError:
            pass

        self.peer_ever_connected = True
        self.last_heartbeat = time.time()
        self._client_reconnect_backoff = self.config.reconnect_interval_min
        log.info(f"[{role_name}] 双向心跳已建立: {sock.getpeername()}")

        def send_loop() -> None:
            """定期发送心跳，连接断开时清理"""
            while self.running and not self._peer_lost_triggered:
                with self._conn_lock:
                    cur = self._active_socket
                if cur is not sock:
                    break
                try:
                    sock.sendall(Protocol.HEARTBEAT)
                except OSError:
                    break
                time.sleep(self.config.heartbeat_interval)
            log.debug(f"[{role_name}] 发送线程退出")
            self._clear_active_socket(sock)
            safe_close_socket(sock)
            if self.running and not self._peer_lost_triggered:
                log.info(f"[{role_name}] TCP 发送连接断开，将尝试重新连接...")

        def recv_loop() -> None:
            """接收循环：解析 HEARTBEAT / PEER_DOWN / SHUTDOWN 消息"""
            recv_buffer = b""
            all_msgs = Protocol.all_messages()

            while self.running and not self._peer_lost_triggered:
                with self._conn_lock:
                    cur = self._active_socket
                if cur is not sock:
                    break
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    log.warning(f"[{role_name}] TCP 连接被重置")
                    break
                except ConnectionAbortedError:
                    log.warning(f"[{role_name}] TCP 连接被本地中止")
                    break
                except ConnectionRefusedError:
                    log.warning(f"[{role_name}] 连接被拒绝")
                    break
                except OSError as e:
                    if not self._shutting_down:
                        log.error(f"[{role_name}] recv OSError (errno={e.errno}): {e}")
                    break
                except Exception as e:
                    log.error(f"[{role_name}] recv 未知异常: {type(e).__name__}: {e}")
                    break

                if not chunk:
                    log.warning(f"[{role_name}] 对方关闭了连接")
                    break

                recv_buffer += chunk

                # 限制接收缓冲区大小，防止内存溢出
                if len(recv_buffer) > self.config.recv_buffer_max:
                    log.warning(f"[{role_name}] 接收缓冲区超限 ({len(recv_buffer)} > "
                                f"{self.config.recv_buffer_max})，断开连接")
                    break

                # 循环解析所有已知消息
                parsed_any = True
                while parsed_any:
                    parsed_any = False
                    for msg in all_msgs:
                        if msg in recv_buffer:
                            idx = recv_buffer.index(msg)
                            recv_buffer = recv_buffer[idx + len(msg):]
                            parsed_any = True

                            if msg == Protocol.HEARTBEAT:
                                self.last_heartbeat = time.time()
                            elif msg == Protocol.PEER_DOWN:
                                log.warning(f"[{role_name}] 收到 PEER_DOWN: 对方 MC 进程已退出！")
                                self._shutting_down = True
                                self._trigger_peer_lost("对方 MC 进程已退出 (PEER_DOWN)")
                                break
                            elif msg == Protocol.SHUTDOWN:
                                log.info(f"[{role_name}] 收到 SHUTDOWN: 对方脚本正常退出")
                                self._shutting_down = True
                                self._trigger_peer_lost("对方脚本正常退出 (SHUTDOWN)")
                                break
                            break

            log.debug(f"[{role_name}] 接收线程退出 (buffer 残留 {len(recv_buffer)} bytes)")
            self._clear_active_socket(sock)
            safe_close_socket(sock)
            if self.running and not self._peer_lost_triggered:
                log.info(f"[{role_name}] TCP 接收连接断开，将尝试重新连接...")

        threading.Thread(target=send_loop, daemon=True,
                         name=f"HBSend-{role_name}").start()
        threading.Thread(target=recv_loop, daemon=True,
                         name=f"HBRecv-{role_name}").start()
        return True

    # ---------- 服务端 ----------
    def _run_server(self) -> None:
        """服务端监听循环"""
        while self.running:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((LOCALHOST, self.config.port))
                sock.listen(1)
                sock.settimeout(5)
                self.server_socket = sock
                log.info(f"服务端已启动，等待对方连接: {LOCALHOST}:{self.config.port}")

                while self.running:
                    try:
                        conn, addr = sock.accept()
                        log.info(f"对方已连接（来自: {addr}）-> 启动双向心跳（Server 侧）")
                        if not self._start_bidirectional_heartbeat(conn, "Server"):
                            safe_close_socket(conn)
                    except socket.timeout:
                        continue
                    except Exception as e:
                        if self.running:
                            log.error(f"accept 错误: {e}")
                        break
            except OSError as e:
                if self.running:
                    log.error(f"服务端启动失败 (端口 {self.config.port}): {e}")
                    time.sleep(5)
            finally:
                self.server_socket = None
                safe_close_socket(sock)

    # ---------- 客户端 ----------
    def _run_client(self) -> None:
        """客户端重连循环"""
        backoff = self._client_reconnect_backoff
        while self.running:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((LOCALHOST, self.config.peer_port))
                log.info(f"已连接到对方: {LOCALHOST}:{self.config.peer_port} -> "
                         f"启动双向心跳（Client 侧）")
                if not self._start_bidirectional_heartbeat(sock, "Client"):
                    safe_close_socket(sock)
                    sock = None
                    while self.running and not self._peer_lost_triggered:
                        with self._conn_lock:
                            if self._active_socket is None:
                                break
                        time.sleep(self.config.heartbeat_interval)
                    backoff = self.config.reconnect_interval_min
                    continue

                while self.running:
                    with self._conn_lock:
                        if self._active_socket is sock:
                            time.sleep(self.config.heartbeat_interval)
                            continue
                    break
                backoff = self.config.reconnect_interval_min
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                elapsed = time.time() - self.startup_time
                if elapsed < self.config.startup_grace_period:
                    log.info(f"等待对方实例启动... "
                             f"({LOCALHOST}:{self.config.peer_port} 尚未就绪，"
                             f"宽限期剩余 {self.config.startup_grace_period - int(elapsed)}s)")
                else:
                    log.warning(f"连接对方失败 ({LOCALHOST}:{self.config.peer_port}): {e}")
                log.info(f"{backoff}秒后重试...")
            finally:
                safe_close_socket(sock)
                if self.running and not self._peer_lost_triggered:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.config.reconnect_interval_max)

    # ---------- 心跳超时检测 ----------
    def _heartbeat_checker(self) -> None:
        """心跳超时是判定对方掉线的兜底机制"""
        while self.running and not self._peer_lost_triggered:
            time.sleep(1)
            elapsed = time.time() - self.last_heartbeat
            since_startup = time.time() - self.startup_time

            if elapsed > self.config.heartbeat_timeout:
                if since_startup < self.config.startup_grace_period and \
                        not self.peer_ever_connected:
                    remain = self.config.startup_grace_period - int(since_startup)
                    if int(since_startup) % 10 == 0:
                        log.info(f"[{int(since_startup)}s] 等待对方实例启动中... "
                                 f"(宽限期剩余 {remain}s)")
                    self.last_heartbeat = time.time()
                    continue
                log.error(f"心跳超时！已 {elapsed:.0f} 秒未收到对方心跳")
                log.error("对方已掉线（未收到明确下线通知，判定为崩溃或网络断开）")
                self._trigger_peer_lost("心跳超时（对方可能崩溃或网络断开）")

    # ---------- 本地进程监控 ----------
    def _local_process_monitor(self) -> None:
        """监控本地 Minecraft 进程存活状态，退出时通知对方"""
        while self.running:
            time.sleep(self.config.heartbeat_interval)
            if not check_process_alive(self.monitored_pid):
                log.warning(f"本地 Minecraft 进程已退出 (PID: {self.monitored_pid})！")
                log.warning("正在通知对方...")
                self._shutting_down = True
                self._send_farewell(Protocol.PEER_DOWN)
                self._clear_active_socket()
                self.running = False
                break

    # ---------- 启动 / 停止 ----------
    def start(self) -> None:
        """启动所有通信线程"""
        log.info(f"本实例监听端口: {self.config.port}")
        log.info(f"对方实例端口: {self.config.peer_port}")
        log.info(f"监控进程 PID: {self.monitored_pid}")
        self.server_thread.start()
        self.client_thread.start()
        self.checker_thread.start()
        self.proc_monitor_thread.start()

    def stop(self, graceful: bool = True) -> None:
        """
        停止连接管理器。

        graceful=True:  发送 SHUTDOWN 通知对方（手动退出）
        graceful=False: 直接关闭（对方掉线触发）
        """
        self._shutting_down = True
        if graceful and not self._peer_lost_triggered:
            self._send_farewell(Protocol.SHUTDOWN)
        self.running = False
        self._clear_active_socket()
        if self.server_socket:
            safe_close_socket(self.server_socket)
            self.server_socket = None

    @property
    def peer_lost_triggered(self) -> bool:
        """是否已触发掉线处理"""
        return self._peer_lost_triggered


# ==================== 服务器连接监控 ====================
class ServerConnectionMonitor:
    """监控 Minecraft 进程的服务器连接状态"""

    def __init__(self, config: AppConfig, peer: PeerConnection,
                 target_pid: int, on_disconnect: Callable[[], None]):
        self.config = config
        self.peer = peer
        self.target_pid = target_pid
        self.on_disconnect = on_disconnect
        self._psutil_permission_warned = False

    def _check_with_fallback(self, pid: int) -> Optional[Tuple[str, int]]:
        """多级检测：psutil → netstat 回退"""
        result = get_minecraft_server_connection(pid)
        if result is not None:
            return result

        try:
            all_conns = get_all_server_connections(pid)
            if len(all_conns) > 0:
                return None
        except Exception:
            pass

        if not self._psutil_permission_warned:
            log.info("[服务器检测] psutil 连接检测权限不足，启用 netstat 备用方案")
            self._psutil_permission_warned = True

        fallback = get_server_connections_fallback(pid)
        for ip, port in fallback:
            if is_likely_game_port(port):
                return (ip, port)
        return None

    def run(self) -> None:
        """服务器连接监控主循环"""
        log.info(f"[服务器检测] 监控线程已启动 (PID: {self.target_pid}, "
                 f"间隔: {self.config.server_check_interval}s)")
        consecutive_disconnects = 0

        while self.peer.running:
            time.sleep(self.config.server_check_interval)
            if not check_process_alive(self.target_pid):
                break

            srv = self._check_with_fallback(self.target_pid)
            if srv is None:
                consecutive_disconnects += 1
                log.warning(
                    f"[服务器检测] 未检测到游戏服务器连接 "
                    f"({consecutive_disconnects}/{self.config.server_check_consecutive}) "
                    f"(PID: {self.target_pid})"
                )
                if consecutive_disconnects >= self.config.server_check_consecutive:
                    log.warning(f"[服务器检测] 客户端已断开服务器连接 (PID: {self.target_pid})")
                    self.on_disconnect()
                    break
            else:
                if consecutive_disconnects > 0:
                    log.info(f"[服务器检测] 服务器连接已恢复: {srv[0]}:{srv[1]}")
                consecutive_disconnects = 0


# ==================== 主应用程序 ====================
class MonitorApp:
    """Minecraft AFK 挂机互保脚本主程序"""

    def __init__(self, config: AppConfig, target_pid: int):
        self.config = config
        self.target_pid = target_pid
        self.peer: Optional[PeerConnection] = None
        self._shutdown_event = threading.Event()

    def _on_peer_lost(self) -> None:
        """对方掉线回调"""
        log.warning("=" * 50)
        log.warning("对方客户端已掉线！")
        log.warning(f"正在结束本地 MC 进程 (PID: {self.target_pid}) ...")
        log.warning("=" * 50)

        send_webhook(self.config.webhook_url,
                     f"[AFK Monitor] 对方掉线，正在终止本地 MC 进程 (PID: {self.target_pid})")

        kill_process(self.target_pid)
        log.info("本地 MC 进程已结束")

        if self.config.restart_command:
            log.info("正在尝试自动重启 Minecraft...")
            restart_minecraft(self.config.restart_command)

        self._shutdown_event.set()

    def _on_server_disconnect(self) -> None:
        """服务器断开连接回调"""
        log.warning("=" * 50)
        log.warning("检测到本地 MC 客户端已断开服务器连接！")
        log.warning(f"正在结束本地 MC 进程 (PID: {self.target_pid}) ...")
        log.warning("=" * 50)

        send_webhook(self.config.webhook_url,
                     f"[AFK Monitor] 服务器连接断开，正在终止本地 MC 进程 (PID: {self.target_pid})")

        if self.peer:
            self.peer._shutting_down = True
            self.peer._send_farewell(Protocol.PEER_DOWN)
        kill_process(self.target_pid)
        log.info("本地 MC 进程已结束")

        if self.config.restart_command:
            log.info("正在尝试自动重启 Minecraft...")
            restart_minecraft(self.config.restart_command)

        self._shutdown_event.set()

    def _signal_handler(self, sig: int, frame) -> None:
        """信号处理：Ctrl+C 优雅退出"""
        log.info("\n收到退出信号 (Ctrl+C)，正在通知对方并关闭...")
        if self.peer:
            self.peer.stop(graceful=True)
        self._shutdown_event.set()

    def run(self) -> None:
        """运行监控主循环"""
        # 注册信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # 创建对等连接管理器
        self.peer = PeerConnection(
            config=self.config,
            monitored_pid=self.target_pid,
            on_peer_lost=self._on_peer_lost
        )
        self.peer.start()

        # 启动服务器连接监控（默认启用）
        if not self.config.no_check_server:
            srv_monitor = ServerConnectionMonitor(
                config=self.config,
                peer=self.peer,
                target_pid=self.target_pid,
                on_disconnect=self._on_server_disconnect
            )
            threading.Thread(
                target=srv_monitor.run,
                daemon=True,
                name="ServerCheckThread"
            ).start()

        # 主循环
        try:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            if self.peer:
                self.peer.stop(graceful=True)
            log.info("脚本已退出。")


# ==================== 入口 ====================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minecraft AFK 挂机互保脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例用法:
  实例A: python afk_monitor.py --instance a
  实例B: python afk_monitor.py --instance b
  手动:  python afk_monitor.py --port 18888 --peer-port 18889 --pid <PID>
  列表:  python afk_monitor.py --list""")
    parser.add_argument("--port", type=int, default=None,
                        help="本实例监听端口号")
    parser.add_argument("--peer-port", type=int, default=None,
                        help="对方实例监听端口号")
    parser.add_argument("--pid", type=int, default=None,
                        help="手动指定 Minecraft 进程 PID")
    parser.add_argument("--auto", action="store_true", default=False,
                        help="全自动检测 Minecraft 进程，配合 --auto-index 使用")
    parser.add_argument("--auto-index", type=int, default=0,
                        help="自动模式下选择第几个进程（0=第1个, 按PID升序，默认0）")
    parser.add_argument("--instance", type=str, default=None, choices=['a', 'b'],
                        help="使用 config.json 中预设的实例快捷配置 (a/b)")
    parser.add_argument("--config", type=str, default="config.json",
                        help="配置文件路径（默认: config.json）")
    parser.add_argument("--list", action="store_true", default=False,
                        help="列出所有 Minecraft 进程后退出")
    parser.add_argument("--heartbeat-interval", type=int, default=-1,
                        help="心跳间隔秒数（覆盖配置文件）")
    parser.add_argument("--heartbeat-timeout", type=int, default=-1,
                        help="心跳超时秒数（覆盖配置文件）")
    parser.add_argument("--server-check-interval", type=int, default=-1,
                        help="服务器连接检测间隔秒数（覆盖配置文件）")
    parser.add_argument("--no-check-server", action="store_true", default=False,
                        help="禁用服务器连接断开检测")
    parser.add_argument("--log-file", type=str, default="",
                        help="日志文件路径（覆盖配置文件）")
    parser.add_argument("--webhook-url", type=str, default="",
                        help="Webhook 通知地址（覆盖配置文件）")
    parser.add_argument("--restart-command", type=str, default="",
                        help="掉线后自动重启 Minecraft 的完整命令行")

    args = parser.parse_args()

    # --list 模式（不需要配置文件）
    if args.list:
        setup_logging()
        processes = find_minecraft_processes()
        if not processes:
            print("未检测到任何 Minecraft 进程。")
        else:
            print(f"\n检测到 {len(processes)} 个 Minecraft 进程:")
            print("=" * 60)
            for pid, name, cmdline in processes:
                cmd_display = cmdline[:150] + "..." if len(cmdline) > 150 else cmdline
                print(f"  PID: {pid:<8} | {name}")
                print(f"  命令行: {cmd_display}\n")
        sys.exit(0)

    # 加载配置
    config = AppConfig.from_args(args)

    # 配置日志
    setup_logging(log_file=config.log_file if config.log_file else None)

    # 验证端口
    if config.port is None or config.peer_port is None:
        if not args.instance and (args.port is None or args.peer_port is None):
            log.error("--port 和 --peer-port 参数是必需的！")
            log.error("或使用 --instance a / --instance b 快捷配置")
            log.error("或确保 config.json 存在并配置了 instance_a / instance_b")
            sys.exit(1)

    # PID 自动检测
    target_pid = args.pid

    if args.auto and target_pid is not None:
        log.warning("同时指定 --auto 和 --pid，优先使用 --pid 手动指定模式。")

    if args.auto and target_pid is None:
        log.info("=" * 50)
        log.info("全自动检测模式")
        log.info("=" * 50)
        log.info("正在扫描 Minecraft Java 进程...")

        processes = find_minecraft_processes()

        if not processes:
            log.error("未检测到任何 Minecraft 进程！")
            log.error("请确保 Minecraft 客户端已启动，然后重新运行。")
            sys.exit(1)

        log.info(f"检测到 {len(processes)} 个 Minecraft 进程:")
        for i, (pid, name, _) in enumerate(processes):
            log.info(f"  [{i}] PID: {pid} | {name}")

        idx = config.auto_index
        if idx < 0 or idx >= len(processes):
            log.error(f"--auto-index {idx} 超出范围！")
            log.error(f"有效范围: 0 ~ {len(processes)-1}")
            log.error("提示: 实例A 用 --auto-index 0，实例B 用 --auto-index 1")
            sys.exit(1)

        target_pid = processes[idx][0]
        log.info(f"✓ 自动绑定: --auto-index={idx} -> PID {target_pid} ({processes[idx][1]})")
        log.info("=" * 50 + "\n")

    if target_pid is None:
        log.error("未指定要监控的进程！")
        log.error("请使用 --auto --auto-index 0 或 --pid <PID>")
        sys.exit(1)

    if not check_process_alive(target_pid):
        log.error(f"进程 PID {target_pid} 不存在或无法访问！")
        log.error("提示: 使用 --list 查看当前所有 MC 进程。")
        sys.exit(1)

    proc = psutil.Process(target_pid)
    log.info(f"监控进程: {proc.name()} (PID: {target_pid})")
    log.info(f"心跳间隔: {config.heartbeat_interval}s, 心跳超时: {config.heartbeat_timeout}s")

    # 服务器连接状态
    log.info("-" * 40)
    log.info("正在检测客户端服务器连接状态...")
    srv = get_minecraft_server_connection(target_pid)
    if srv:
        log.info(f"✓ 已连接至游戏服务器: {srv[0]}:{srv[1]}")
    else:
        log.warning("✗ 当前未检测到游戏服务器连接")
    all_c = get_all_server_connections(target_pid)
    if all_c:
        log.info(f"  检测到 {len(all_c)} 个远程连接（含认证/API 等）:")
        for ip, port in all_c:
            tag = " [游戏]" if is_likely_game_port(port) else " [非游戏]"
            log.info(f"    - {ip}:{port}{tag}")
    log.info("-" * 40)
    if config.no_check_server:
        log.info("服务器连接检测已禁用")
    else:
        log.info(f"服务器连接检测已启用 (间隔: {config.server_check_interval}s)")
    log.info("")

    # 启动主程序
    app = MonitorApp(config, target_pid)
    app.run()


if __name__ == "__main__":
    main()