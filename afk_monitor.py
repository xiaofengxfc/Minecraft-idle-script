#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft AFK 挂机互保脚本
============================
功能：两个 Minecraft 客户端在同一台机器上运行时，通过本地 TCP 互联互相检测在线状态。
      一方掉线（进程退出或网络断开），另一方自动结束自己的 Minecraft 客户端进程。

使用方式：
    实例A: python afk_monitor.py --port 18888 --peer-port 18889 --auto --auto-index 0
    实例B: python afk_monitor.py --port 18889 --peer-port 18888 --auto --auto-index 1
    手动:  python afk_monitor.py --port 18888 --peer-port 18889 --pid <MC进程PID>

依赖：psutil（脚本会自动尝试安装）
"""

import socket
import threading
import time
import os
import sys
import signal
import argparse
import subprocess
import logging
from typing import List, Tuple, Optional

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)


# ==================== 自动安装依赖 ====================
def ensure_psutil():
    """确保 psutil 已安装，否则自动安装"""
    try:
        import psutil
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
            import psutil
            return psutil
        except Exception as e:
            log.error(f"psutil 安装失败: {e}")
            log.error("请手动执行: pip install psutil")
            sys.exit(1)


psutil = ensure_psutil()

# ==================== 配置常量 ====================
HEARTBEAT_INTERVAL = 3          # 发送心跳间隔
HEARTBEAT_TIMEOUT = 15          # 收不到心跳的超时时间
RECONNECT_INTERVAL_MIN = 2      # 重连最短间隔
RECONNECT_INTERVAL_MAX = 30     # 重连最长间隔（指数退避上限）
LOCALHOST = "127.0.0.1"

# ---- 协议消息 ----
HEARTBEAT_MSG = b"ALIVE\n"      # 心跳消息 — 刷新对方 last_heartbeat
PEER_DOWN_MSG = b"PEER_DOWN\n"  # 我方 MC 进程已退出 — 对方收到后应立即杀进程
SHUTDOWN_MSG = b"SHUTDOWN\n"    # 脚本正常退出 (Ctrl+C) — 对方可安全退出

STARTUP_GRACE_PERIOD = 90       # 启动宽限期：期间不因连接失败判定超时

# TCP 优化常量（代理环境兼容）
TCP_KEEPIDLE = 10               # TCP keepalive 空闲秒数
TCP_KEEPINTVL = 5               # TCP keepalive 探测间隔
TCP_KEEPCNT = 3                 # TCP keepalive 探测次数


# ==================== Minecraft 进程自动检测 ====================
def find_minecraft_processes() -> List[Tuple[int, str, str]]:
    """
    扫描系统中所有正在运行的 Minecraft Java 进程。
    返回按 PID 升序排列的 (pid, process_name, command_line) 列表。
    """
    minecraft_processes = []

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

            display = name
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
def get_minecraft_server_connection(pid: int) -> Optional[Tuple[str, int]]:
    """获取 Minecraft 进程的主服务器连接（排除本地回环）"""
    try:
        for conn in psutil.Process(pid).net_connections(kind='tcp'):
            if conn.status != 'ESTABLISHED' or not conn.raddr:
                continue
            ip = conn.raddr.ip
            if ip.startswith('127.') or ip in ('::1', '0.0.0.0'):
                continue
            return (ip, conn.raddr.port)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return None


def get_all_server_connections(pid: int) -> List[Tuple[str, int]]:
    """获取所有远程服务器连接"""
    result = []
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


# ==================== 进程管理 ====================
def check_process_alive(pid):
    try:
        return psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process(pid):
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


# ==================== 对等连接管理器 ====================
class PeerConnection:
    """
    管理与对等脚本的 TCP 心跳连接。

    协议设计：
    - HEARTBEAT_MSG (ALIVE\\n): 定期心跳，刷新 last_heartbeat
    - PEER_DOWN_MSG (PEER_DOWN\\n): 我方 MC 进程已退出，对方收到后立即杀进程
    - SHUTDOWN_MSG (SHUTDOWN\\n): 脚本正常退出，对方可安全退出

    判定对方退出的三种路径（满足任一即触发）：
    1. 收到 PEER_DOWN_MSG 或 SHUTDOWN_MSG → 立即判定（0s 延迟）
    2. 心跳超时（连续 HEARTBEAT_TIMEOUT 秒未收到 ALIVE）→ 兜底判定
    3. 服务器连接断开检测（可选）→ 检测自身 MC 是否断开了服务器
    """

    def __init__(self, port, peer_port, monitored_pid, on_peer_lost):
        self.port = port
        self.peer_port = peer_port
        self.monitored_pid = monitored_pid
        self.on_peer_lost = on_peer_lost

        self.server_socket = None
        self.last_heartbeat = time.time()
        self.startup_time = time.time()
        self.peer_ever_connected = False
        self.running = True
        self._conn_lock = threading.Lock()
        self._active_socket = None
        self._peer_lost_triggered = False
        self._client_reconnect_backoff = RECONNECT_INTERVAL_MIN
        self._shutting_down = False          # 标记正在优雅关闭，抑制后续错误日志

        # 各线程
        self.server_thread = threading.Thread(
            target=self._run_server, daemon=True, name="ServerThread")
        self.client_thread = threading.Thread(
            target=self._run_client, daemon=True, name="ClientThread")
        self.checker_thread = threading.Thread(
            target=self._heartbeat_checker, daemon=True, name="CheckerThread")
        self.proc_monitor_thread = threading.Thread(
            target=self._local_process_monitor, daemon=True, name="ProcMonitorThread")

    # ---------- TCP Socket 优化 ----------
    @staticmethod
    def _optimize_tcp_socket(sock: socket.socket):
        """
        TCP socket 优化：TCP_NODELAY + SO_KEEPALIVE + 平台参数。
        """
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
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPIDLE)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPINTVL)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPCNT)
            else:
                SIO_KEEPALIVE_VALS = 0x98000004
                sock.ioctl(
                    SIO_KEEPALIVE_VALS,
                    (1, TCP_KEEPIDLE * 1000, TCP_KEEPINTVL * 1000)
                )
        except (OSError, AttributeError, ImportError):
            pass

    # ---------- 告别消息发送 ----------
    def _send_farewell(self, msg: bytes):
        """
        通过活跃 socket 发送告别消息（PEER_DOWN 或 SHUTDOWN）。
        最多重试 3 次，每次间隔 0.5s，确保对方能收到。
        """
        sock = None
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

    # ---------- 活跃连接管理 ----------
    def _try_claim_active_socket(self, sock) -> bool:
        with self._conn_lock:
            if self._active_socket is None and not self._peer_lost_triggered:
                self._active_socket = sock
                return True
            return False

    def _clear_active_socket(self, sock=None):
        with self._conn_lock:
            if sock is None or self._active_socket is sock:
                self._active_socket = None

    # ---------- 判定对方掉线 ----------
    def _trigger_peer_lost(self, reason: str = ""):
        """
        线程安全地触发 on_peer_lost，确保只执行一次。

        触发来源：
        1. 收到 PEER_DOWN_MSG → reason = "对方 MC 进程已退出"
        2. 收到 SHUTDOWN_MSG → reason = "对方脚本正常退出"
        3. 心跳超时 → reason = "心跳超时"
        """
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
    def _start_bidirectional_heartbeat(self, sock, role_name: str):
        """
        在已建立的 TCP 连接上启动双向心跳。
        先到先得抢占活跃连接，抢占失败返回 False。
        """
        if not self._try_claim_active_socket(sock):
            log.debug(f"[{role_name}] 连接未被接纳（已有活跃连接），关闭")
            return False

        self._optimize_tcp_socket(sock)
        try:
            sock.settimeout(HEARTBEAT_INTERVAL + 2)
        except OSError:
            pass

        self.peer_ever_connected = True
        self.last_heartbeat = time.time()
        self._client_reconnect_backoff = RECONNECT_INTERVAL_MIN
        log.info(f"[{role_name}] 双向心跳已建立: {sock.getpeername()}")

        def send_loop():
            """定期发送心跳，连接断开时清理等待重连"""
            while self.running and not self._peer_lost_triggered:
                with self._conn_lock:
                    cur = self._active_socket
                if cur is not sock:
                    break
                try:
                    sock.sendall(HEARTBEAT_MSG)
                except OSError:
                    break
                time.sleep(HEARTBEAT_INTERVAL)
            log.debug(f"[{role_name}] 发送线程退出")
            self._clear_active_socket(sock)
            try:
                sock.close()
            except Exception:
                pass
            if self.running and not self._peer_lost_triggered:
                log.info(f"[{role_name}] TCP 发送连接断开，将尝试重新连接...")

        def recv_loop():
            """
            接收循环：解析 HEARTBEAT、PEER_DOWN、SHUTDOWN 三种消息。

            - ALIVE\\n: 刷新 last_heartbeat
            - PEER_DOWN\\n: 对方 MC 进程已退出 → 立即触发 _trigger_peer_lost
            - SHUTDOWN\\n: 对方脚本正常退出 → 立即触发 _trigger_peer_lost
            """
            recv_buffer = b""
            all_msgs = [HEARTBEAT_MSG, PEER_DOWN_MSG, SHUTDOWN_MSG]

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
                    log.warning(f"[{role_name}] TCP 连接被重置 — 代理或对端可能已断开")
                    break
                except ConnectionAbortedError:
                    log.warning(f"[{role_name}] TCP 连接被本地中止 — 可能是代理干扰")
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
                    log.warning(f"[{role_name}] 对方关闭了连接 (recv 返回空)")
                    break

                recv_buffer += chunk

                # 循环解析已知消息（连续处理 buffer 中可能的多条消息）
                parsed_any = True
                while parsed_any:
                    parsed_any = False
                    for msg in all_msgs:
                        if msg in recv_buffer:
                            idx = recv_buffer.index(msg)
                            recv_buffer = recv_buffer[idx + len(msg):]
                            parsed_any = True

                            if msg is HEARTBEAT_MSG:
                                self.last_heartbeat = time.time()
                            elif msg is PEER_DOWN_MSG:
                                log.warning(f"[{role_name}] 收到 PEER_DOWN: 对方 MC 进程已退出！")
                                self._shutting_down = True
                                self._trigger_peer_lost("对方 MC 进程已退出 (PEER_DOWN)")
                                break
                            elif msg is SHUTDOWN_MSG:
                                log.info(f"[{role_name}] 收到 SHUTDOWN: 对方脚本正常退出")
                                self._shutting_down = True
                                self._trigger_peer_lost("对方脚本正常退出 (SHUTDOWN)")
                                break
                            # 继续检查 buffer 中剩余数据
                            break  # 跳出 for 循环，重新 while 检测是否有更多消息

            log.debug(f"[{role_name}] 接收线程退出 (buffer 残留 {len(recv_buffer)} bytes)")
            self._clear_active_socket(sock)
            try:
                sock.close()
            except Exception:
                pass
            if self.running and not self._peer_lost_triggered:
                log.info(f"[{role_name}] TCP 接收连接断开，将尝试重新连接...")

        threading.Thread(target=send_loop, daemon=True,
                         name=f"HBSend-{role_name}").start()
        threading.Thread(target=recv_loop, daemon=True,
                         name=f"HBRecv-{role_name}").start()
        return True

    # ---------- 服务端 ----------
    def _run_server(self):
        while self.running:
            try:
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind((LOCALHOST, self.port))
                self.server_socket.listen(1)
                self.server_socket.settimeout(5)
                log.info(f"服务端已启动，等待对方连接: {LOCALHOST}:{self.port}")

                while self.running:
                    try:
                        conn, addr = self.server_socket.accept()
                        log.info(f"对方已连接（来自: {addr}）-> 尝试启动双向心跳（Server 侧）")
                        if not self._start_bidirectional_heartbeat(conn, "Server"):
                            try:
                                conn.close()
                            except Exception:
                                pass
                    except socket.timeout:
                        continue
                    except Exception as e:
                        if self.running:
                            log.error(f"accept 错误: {e}")
                        break
            except OSError as e:
                if self.running:
                    log.error(f"服务端启动失败 (端口 {self.port}): {e}")
                    time.sleep(5)
            finally:
                if self.server_socket:
                    try:
                        self.server_socket.close()
                    except Exception:
                        pass
                    self.server_socket = None

    # ---------- 客户端 ----------
    def _run_client(self):
        backoff = self._client_reconnect_backoff
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((LOCALHOST, self.peer_port))
                log.info(f"已连接到对方: {LOCALHOST}:{self.peer_port} -> 尝试启动双向心跳（Client 侧）")
                if not self._start_bidirectional_heartbeat(sock, "Client"):
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                    while self.running and not self._peer_lost_triggered:
                        with self._conn_lock:
                            if self._active_socket is None:
                                break
                        time.sleep(HEARTBEAT_INTERVAL)
                    backoff = RECONNECT_INTERVAL_MIN
                    continue
                while self.running:
                    with self._conn_lock:
                        if self._active_socket is sock:
                            time.sleep(HEARTBEAT_INTERVAL)
                            continue
                    break
                backoff = RECONNECT_INTERVAL_MIN
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                elapsed = time.time() - self.startup_time
                if elapsed < STARTUP_GRACE_PERIOD:
                    log.info(f"等待对方实例启动... "
                             f"({LOCALHOST}:{self.peer_port} 尚未就绪，"
                             f"宽限期剩余 {STARTUP_GRACE_PERIOD - int(elapsed)}s)")
                else:
                    log.warning(f"连接对方失败 ({LOCALHOST}:{self.peer_port}): {e}")
                log.info(f"{backoff}秒后重试...")
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                if self.running and not self._peer_lost_triggered:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_INTERVAL_MAX)

    # ---------- 心跳超时检测（兜底判定）----------
    def _heartbeat_checker(self):
        """
        心跳超时是判定对方掉线的兜底机制。
        正常情况应该通过 PEER_DOWN/SHUTDOWN 消息即时判定，
        心跳超时仅在连接意外断开（进程崩溃、网络断开）时生效。
        """
        while self.running and not self._peer_lost_triggered:
            time.sleep(1)
            elapsed = time.time() - self.last_heartbeat
            since_startup = time.time() - self.startup_time

            if elapsed > HEARTBEAT_TIMEOUT:
                if since_startup < STARTUP_GRACE_PERIOD and not self.peer_ever_connected:
                    remain = STARTUP_GRACE_PERIOD - int(since_startup)
                    if int(since_startup) % 10 == 0:
                        log.info(f"[{int(since_startup)}s] 等待对方实例启动中... "
                                 f"(宽限期剩余 {remain}s)")
                    self.last_heartbeat = time.time()
                    continue
                log.error(f"心跳超时！已 {elapsed:.0f} 秒未收到对方心跳")
                log.error("对方已掉线（未收到明确下线通知，判定为崩溃或网络断开）")
                self._trigger_peer_lost("心跳超时（对方可能崩溃或网络断开）")

    # ---------- 本地进程监控 ----------
    def _local_process_monitor(self):
        """
        监控本地 Minecraft 进程存活状态。
        一旦检测到本地 MC 进程退出，立即通过活跃 socket
        发送 PEER_DOWN_MSG 通知对方，确保对方即时响应。
        """
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not check_process_alive(self.monitored_pid):
                log.warning(f"本地 Minecraft 进程已退出 (PID: {self.monitored_pid})！")
                log.warning("正在通知对方...")
                self._shutting_down = True
                # 发送 PEER_DOWN 让对方立即知晓（不等待心跳超时）
                self._send_farewell(PEER_DOWN_MSG)
                self._clear_active_socket()
                self.running = False
                break

    # ---------- 启动 / 停止 ----------
    def start(self):
        log.info(f"本实例监听端口: {self.port}")
        log.info(f"对方实例端口: {self.peer_port}")
        log.info(f"监控进程 PID: {self.monitored_pid}")
        self.server_thread.start()
        self.client_thread.start()
        self.checker_thread.start()
        self.proc_monitor_thread.start()

    def stop(self, graceful=True):
        """
        停止连接管理器。

        graceful=True:  发送 SHUTDOWN_MSG 通知对方（优雅退出）
        graceful=False: 直接关闭（由 on_peer_lost 触发的强制退出）
        """
        self._shutting_down = True
        if graceful and not self._peer_lost_triggered:
            self._send_farewell(SHUTDOWN_MSG)
        self.running = False
        self._clear_active_socket()
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
            self.server_socket = None


# ==================== 主程序 ====================
def main():
    global HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT

    parser = argparse.ArgumentParser(
        description="Minecraft AFK 挂机互保脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例用法:
  实例A: python afk_monitor.py --port 18888 --peer-port 18889 --auto --auto-index 0
  实例B: python afk_monitor.py --port 18889 --peer-port 18888 --auto --auto-index 1
  手动:  python afk_monitor.py --port 18888 --peer-port 18889 --pid <PID>""")
    parser.add_argument("--port", type=int, default=None,
                        help="本实例监听端口号")
    parser.add_argument("--peer-port", type=int, default=None,
                        help="对方实例监听端口号")
    parser.add_argument("--pid", type=int, default=None,
                        help="手动指定 Minecraft 进程 PID")
    parser.add_argument("--auto", action="store_true", default=False,
                        help="全自动检测 Minecraft 进程，配合 --auto-index 使用")
    parser.add_argument("--auto-index", type=int, default=0,
                        help="自动模式下选择第几个进程（0=第1个, 1=第2个, 按PID升序，默认0）")
    parser.add_argument("--list", action="store_true", default=False,
                        help="列出所有 Minecraft 进程后退出")
    parser.add_argument("--heartbeat-interval", type=int, default=HEARTBEAT_INTERVAL,
                        help=f"心跳间隔秒数（默认: {HEARTBEAT_INTERVAL}）")
    parser.add_argument("--heartbeat-timeout", type=int, default=HEARTBEAT_TIMEOUT,
                        help=f"心跳超时秒数（默认: {HEARTBEAT_TIMEOUT}）")
    parser.add_argument("--no-check-server", action="store_true", default=False,
                        help="禁用服务器连接断开检测（默认启用）")
    parser.add_argument("--server-check-interval", type=int, default=10,
                        help="服务器连接检测间隔秒数（默认: 10）")

    args = parser.parse_args()
    HEARTBEAT_INTERVAL = args.heartbeat_interval
    HEARTBEAT_TIMEOUT = args.heartbeat_timeout

    # --list 模式
    if args.list:
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

    if args.port is None or args.peer_port is None:
        log.error("--port 和 --peer-port 参数是必需的！")
        log.error("用法: python afk_monitor.py --port <本机端口> --peer-port <对方端口> "
                  "[--auto --auto-index N]")
        sys.exit(1)

    # ========== PID 自动检测 ==========
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

        idx = args.auto_index
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
    log.info(f"心跳间隔: {HEARTBEAT_INTERVAL}s, 心跳超时: {HEARTBEAT_TIMEOUT}s")

    # 服务器连接状态
    log.info("-" * 40)
    log.info("正在检测客户端服务器连接状态...")
    srv = get_minecraft_server_connection(target_pid)
    if srv:
        log.info(f"✓ 已连接至服务器: {srv[0]}:{srv[1]}")
    else:
        log.warning("✗ 当前未连接至远程服务器")
    all_c = get_all_server_connections(target_pid)
    if len(all_c) > 1:
        log.info(f"  检测到 {len(all_c)} 个远程连接:")
        for ip, port in all_c:
            log.info(f"    - {ip}:{port}")
    log.info("-" * 40)
    if args.no_check_server:
        log.info("服务器连接检测已禁用 (使用 --no-check-server 禁用)")
    else:
        log.info(f"服务器连接检测已启用 (间隔: {args.server_check_interval}s)")
    log.info("")

    # ========== 回调函数 ==========
    def on_peer_lost():
        log.warning("=" * 50)
        log.warning("对方客户端已掉线！")
        log.warning(f"正在结束本地 MC 进程 (PID: {target_pid}) ...")
        log.warning("=" * 50)
        kill_process(target_pid)
        log.info("本地 MC 进程已结束，脚本即将退出。")
        os._exit(0)

    def on_server_disconnect():
        log.warning("=" * 50)
        log.warning("检测到本地 MC 客户端已断开服务器连接！")
        log.warning(f"正在结束本地 MC 进程 (PID: {target_pid}) ...")
        log.warning("=" * 50)
        # 先通知对方我方即将退出
        peer._shutting_down = True
        peer._send_farewell(PEER_DOWN_MSG)
        kill_process(target_pid)
        log.info("本地 MC 进程已结束，脚本即将退出。")
        os._exit(0)

    # ========== 创建连接管理器 ==========
    peer = PeerConnection(
        port=args.port,
        peer_port=args.peer_port,
        monitored_pid=target_pid,
        on_peer_lost=on_peer_lost)

    def signal_handler(sig, frame):
        log.info("\n收到退出信号 (Ctrl+C)，正在通知对方并关闭...")
        peer.stop(graceful=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    peer.start()

    # ========== 服务器连接监控（默认启用）==========
    if not args.no_check_server:
        def server_monitor(pid, interval, peer_obj, cb):
            log.info(f"[服务器检测] 监控线程已启动 (PID: {pid}, 间隔: {interval}s)")
            while peer_obj.running:
                time.sleep(interval)
                if not check_process_alive(pid):
                    break
                if get_minecraft_server_connection(pid) is None:
                    log.warning(f"[服务器检测] 客户端已断开服务器连接 (PID: {pid})")
                    cb()
                    break

        threading.Thread(
            target=server_monitor,
            args=(target_pid, args.server_check_interval, peer, on_server_disconnect),
            daemon=True, name="ServerCheckThread").start()

    # ========== 主循环 ==========
    try:
        while peer.running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        peer.stop(graceful=True)
        log.info("脚本已退出。")


if __name__ == "__main__":
    main()