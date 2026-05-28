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
HEARTBEAT_MSG = b"ALIVE\n"
STARTUP_GRACE_PERIOD = 90       # 启动宽限期：期间不因连接失败判定超时

# TCP 优化常量（代理环境兼容）
TCP_KEEPIDLE = 10               # TCP keepalive 空闲秒数（Windows 默认 2 小时，此处缩短）
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

    核心设计：
    - 双方各自启动 TCP Server 监听自身端口
    - 双方各自作为 Client 去连接对方端口
    - 任意一条 TCP 连接建立后，在该连接上进行双向心跳收发
    - 发送线程定期发送 HEARTBEAT_MSG，接收线程持续读取并刷新 last_heartbeat
    - TCP 连接断开时只清理连接，由 Client 重连机制自动恢复
    - 心跳超时（默认 15s 收不到心跳）作为唯一判定对方掉线的机制
    - 启动宽限期（默认 90s）内不因连接失败/心跳超时判定掉线
    """

    def __init__(self, port, peer_port, monitored_pid, on_peer_lost):
        self.port = port
        self.peer_port = peer_port
        self.monitored_pid = monitored_pid
        self.on_peer_lost = on_peer_lost

        self.server_socket = None
        self.last_heartbeat = time.time()
        self.startup_time = time.time()
        self.peer_ever_connected = False      # 是否有过任意 TCP 连接
        self.running = True
        self._conn_lock = threading.Lock()     # 保护活跃连接和防重复触发
        self._active_socket = None             # 当前活跃的双向心跳 socket
        self._peer_lost_triggered = False      # 防止重复触发 on_peer_lost
        self._client_reconnect_backoff = RECONNECT_INTERVAL_MIN

        # 各线程
        self.server_thread = threading.Thread(
            target=self._run_server, daemon=True, name="ServerThread")
        self.client_thread = threading.Thread(
            target=self._run_client, daemon=True, name="ClientThread")
        self.checker_thread = threading.Thread(
            target=self._heartbeat_checker, daemon=True, name="CheckerThread")
        self.proc_monitor_thread = threading.Thread(
            target=self._local_process_monitor, daemon=True, name="ProcMonitorThread")

    # ---------- TCP Socket 优化（代理环境兼容）----------
    @staticmethod
    def _optimize_tcp_socket(sock: socket.socket):
        """
        对 TCP socket 进行代理环境兼容性优化：
        1. TCP_NODELAY: 禁用 Nagle 算法，心跳小包立即发送不等待缓冲合并
        2. SO_KEEPALIVE: 启用 TCP keepalive，防止代理/NAT 静默断开空闲连接
        3. 平台相关 keepalive 参数 (TCP_KEEPIDLE/INTVL/CNT):
           Linux/WSL/macOS 直接设置，Windows 通过 SIO_KEEPALIVE_VALS 设置
        """
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass  # 某些环境不支持，忽略

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass  # 不支持则跳过

        # 平台相关 keepalive 参数
        try:
            # Linux / WSL / macOS
            if sys.platform != 'win32':
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPIDLE)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPINTVL)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPCNT)
            else:
                # Windows: 使用 SIO_KEEPALIVE_VALS
                # Python socket.ioctl 的第二个参数需要是 3 元组
                # 而非 bytes，直接传入 (onoff, keepalivetime_ms, keepaliveinterval_ms)
                SIO_KEEPALIVE_VALS = 0x98000004
                sock.ioctl(
                    SIO_KEEPALIVE_VALS,
                    (1, TCP_KEEPIDLE * 1000, TCP_KEEPINTVL * 1000)
                )
        except (OSError, AttributeError, ImportError):
            # 平台不支持或权限不足，使用 OS 默认 keepalive（Windows 默认 2h）
            pass

    # ---------- 活跃连接管理 ----------
    def _set_active_socket(self, sock):
        """设置活跃 socket，如果已有则关闭旧的（保留新连接）"""
        with self._conn_lock:
            old = self._active_socket
            self._active_socket = sock
            if old and old is not sock:
                try:
                    old.close()
                except Exception:
                    pass

    def _clear_active_socket(self, sock=None):
        """清除活跃 socket（仅当匹配时清除）"""
        with self._conn_lock:
            if sock is None or self._active_socket is sock:
                self._active_socket = None

    def _trigger_peer_lost(self, reason: str = ""):
        """
        线程安全地触发 on_peer_lost，确保只执行一次。
        由心跳超时检测器调用（连续15秒未收到心跳时），是判定对方掉线的唯一入口。
        """
        with self._conn_lock:
            if self._peer_lost_triggered:
                return
            self._peer_lost_triggered = True
        if reason:
            log.warning(f"检测到连接断开: {reason}")
        log.warning("对方已断开连接，正在结束本地 Minecraft 客户端...")
        self.running = False
        self.on_peer_lost()

    # ---------- 双向心跳 ----------
    def _start_bidirectional_heartbeat(self, sock, role_name: str):
        """
        在已建立的 TCP 连接上启动双向心跳。
        role_name: "Server" 或 "Client"，仅用于日志。
        """
        # 先优化 TCP socket（NODELAY + Keepalive），再开始心跳
        self._optimize_tcp_socket(sock)
        try:
            sock.settimeout(HEARTBEAT_INTERVAL + 2)
        except OSError:
            pass

        self.peer_ever_connected = True
        self._set_active_socket(sock)
        self.last_heartbeat = time.time()
        self._client_reconnect_backoff = RECONNECT_INTERVAL_MIN  # 重置退避
        log.info(f"[{role_name}] 双向心跳已建立: {sock.getpeername()}")

        def send_loop():
            while self.running and not self._peer_lost_triggered:
                with self._conn_lock:
                    cur = self._active_socket
                if cur is not sock:
                    break
                try:
                    sock.sendall(HEARTBEAT_MSG)
                except OSError:
                    break
                except Exception:
                    break
                time.sleep(HEARTBEAT_INTERVAL)
            # 发送失败，清理连接，由心跳超时检测器最终判定是否掉线
            log.debug(f"[{role_name}] 发送线程退出")
            self._clear_active_socket(sock)
            try:
                sock.close()
            except Exception:
                pass
            # 不立即判定对方掉线！交由 _heartbeat_checker 超时机制兜底
            # 在此期间 client 重连机制会尝试恢复连接
            if self.running and not self._peer_lost_triggered:
                log.info(f"[{role_name}] TCP 发送连接断开，将尝试重新连接...")

        def recv_loop():
            """
            优化版 TCP 接收循环（代理环境兼容）：

            问题：代理软件可能对 TCP 流进行分片/重组，导致单次 recv() 只收到
                  部分数据而非完整心跳消息，或代理/NAT 静默断开连接后产生模糊
                  的 OSError，需要区分处理。

            改进：
            1. 使用 recv_buffer 累积字节，按 HEARTBEAT_MSG 分隔符逐条解析
            2. 收到任意 HEARTBEAT_MSG 即刷新 last_heartbeat
            3. 代理软件典型错误码精确区分：
               - ConnectionResetError (10054): 对端重置连接 → 真正断开
               - ConnectionAbortedError (10053): 本地软件中止 → 可能是代理干扰
               - 其他 OSError → 记录详细 errno 但不立即判定
            """
            recv_buffer = b""
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
                    log.warning(f"[{role_name}] TCP 连接被重置 (ConnectionResetError)"
                                f" — 代理或对端可能已断开")
                    break
                except ConnectionAbortedError:
                    log.warning(f"[{role_name}] TCP 连接被本地中止 (ConnectionAbortedError)"
                                f" — 可能是代理软件干扰")
                    break
                except ConnectionRefusedError:
                    log.warning(f"[{role_name}] 连接被拒绝 (ConnectionRefusedError)")
                    break
                except OSError as e:
                    log.error(f"[{role_name}] recv OSError (errno={e.errno}): {e}")
                    break
                except Exception as e:
                    log.error(f"[{role_name}] recv 未知异常: {type(e).__name__}: {e}")
                    break

                if not chunk:
                    log.warning(f"[{role_name}] 对方关闭了连接 (recv 返回空)")
                    break

                # 累积数据并解析心跳
                recv_buffer += chunk
                while HEARTBEAT_MSG in recv_buffer:
                    idx = recv_buffer.index(HEARTBEAT_MSG)
                    recv_buffer = recv_buffer[idx + len(HEARTBEAT_MSG):]
                    self.last_heartbeat = time.time()

            # 接收失败或连接断开，清理连接，由心跳超时检测器最终判定是否掉线
            log.debug(f"[{role_name}] 接收线程退出 (buffer 残留 {len(recv_buffer)} bytes)")
            self._clear_active_socket(sock)
            try:
                sock.close()
            except Exception:
                pass
            # 不立即判定对方掉线！交由 _heartbeat_checker 超时机制兜底
            # 在此期间 client 重连机制会尝试恢复连接
            if self.running and not self._peer_lost_triggered:
                log.info(f"[{role_name}] TCP 接收连接断开，将尝试重新连接...")

        threading.Thread(target=send_loop, daemon=True,
                         name=f"HBSend-{role_name}").start()
        threading.Thread(target=recv_loop, daemon=True,
                         name=f"HBRecv-{role_name}").start()

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
                        log.info(f"对方已连接（来自: {addr}）-> 启动双向心跳（Server 侧）")
                        self._start_bidirectional_heartbeat(conn, "Server")
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
                log.info(f"已连接到对方: {LOCALHOST}:{self.peer_port} -> 启动双向心跳（Client 侧）")
                self._start_bidirectional_heartbeat(sock, "Client")
                # 双向心跳线程接管后，客户端连接循环等待直到连接断开
                while self.running:
                    with self._conn_lock:
                        if self._active_socket is sock:
                            time.sleep(HEARTBEAT_INTERVAL)
                            continue
                    break
                # 连接断开，重置退避时间以快速重连
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
                if self.running:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_INTERVAL_MAX)

    # ---------- 超时检测（唯一判定机制）----------
    def _heartbeat_checker(self):
        """心跳超时是判定对方掉线的唯一机制，TCP 连接断开不会直接触发杀进程"""
        while self.running and not self._peer_lost_triggered:
            time.sleep(1)
            elapsed = time.time() - self.last_heartbeat
            since_startup = time.time() - self.startup_time

            if elapsed > HEARTBEAT_TIMEOUT:
                # 启动宽限期：即使超时也不判定，给对方充足启动时间
                if since_startup < STARTUP_GRACE_PERIOD and not self.peer_ever_connected:
                    remain = STARTUP_GRACE_PERIOD - int(since_startup)
                    if int(since_startup) % 10 == 0:
                        log.info(f"[{int(since_startup)}s] 等待对方实例启动中... "
                                 f"(宽限期剩余 {remain}s)")
                    # 重置计时器，避免宽限期内误判
                    self.last_heartbeat = time.time()
                    continue
                log.error(f"心跳超时！已 {elapsed:.0f} 秒未收到对方心跳")
                log.error("对方已掉线，正在结束本地 Minecraft 客户端...")
                self._trigger_peer_lost("心跳超时")

    # ---------- 本地进程监控 ----------
    def _local_process_monitor(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not check_process_alive(self.monitored_pid):
                log.warning(f"本地 Minecraft 进程已退出 (PID: {self.monitored_pid})")
                log.warning("主动断开连接，通知对方...")
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

    def stop(self):
        self.running = False
        self._clear_active_socket()
        # 关闭 server socket 以释放端口
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
    parser.add_argument("--check-server", action="store_true", default=False,
                        help="启用服务器连接断开检测")
    parser.add_argument("--server-check-interval", type=int, default=10,
                        help="服务器连接检测间隔秒数（默认: 10）")

    args = parser.parse_args()
    HEARTBEAT_INTERVAL = args.heartbeat_interval
    HEARTBEAT_TIMEOUT = args.heartbeat_timeout

    # --list 模式（不需要 port 和 peer-port）
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

    # 非 list 模式必须提供 port 和 peer-port
    if args.port is None or args.peer_port is None:
        log.error("--port 和 --peer-port 参数是必需的！")
        log.error("用法: python afk_monitor.py --port <本机端口> --peer-port <对方端口> [--auto --auto-index N]")
        sys.exit(1)

    # ========== PID 自动检测逻辑 ==========
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
    log.info(f"心跳间隔: {HEARTBEAT_INTERVAL}s, 超时: {HEARTBEAT_TIMEOUT}s")

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
    if args.check_server:
        log.info(f"服务器连接检测已启用 (间隔: {args.server_check_interval}s)")
    log.info("")

    def on_peer_lost():
        log.warning("=" * 50)
        log.warning("检测到对方客户端掉线！")
        log.warning(f"正在结束本地 MC 进程 (PID: {target_pid}) ...")
        log.warning("=" * 50)
        kill_process(target_pid)
        log.info("本地 MC 进程已结束，脚本即将退出。")
        os._exit(0)

    def on_server_disconnect():
        log.warning("=" * 50)
        log.warning("检测到 MC 客户端已断开服务器连接！")
        log.warning(f"正在结束本地 MC 进程 (PID: {target_pid}) ...")
        log.warning("=" * 50)
        kill_process(target_pid)
        log.info("本地 MC 进程已结束，脚本即将退出。")
        os._exit(0)

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

    peer = PeerConnection(
        port=args.port,
        peer_port=args.peer_port,
        monitored_pid=target_pid,
        on_peer_lost=on_peer_lost)

    def signal_handler(sig, frame):
        log.info("\n收到退出信号，正在关闭...")
        peer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    peer.start()

    if args.check_server:
        threading.Thread(
            target=server_monitor,
            args=(target_pid, args.server_check_interval, peer, on_server_disconnect),
            daemon=True, name="ServerCheckThread").start()

    try:
        while peer.running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        peer.stop()
        log.info("脚本已退出。")


if __name__ == "__main__":
    main()