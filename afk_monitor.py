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
HEARTBEAT_INTERVAL = 3
HEARTBEAT_TIMEOUT = 15
RECONNECT_INTERVAL = 5
LOCALHOST = "127.0.0.1"
HEARTBEAT_MSG = b"ALIVE\n"

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

    # 按 PID 升序排序，确保每次检测结果稳定一致
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
    """管理与对等脚本的 TCP 心跳连接"""

    def __init__(self, port, peer_port, monitored_pid, on_peer_lost):
        self.port = port
        self.peer_port = peer_port
        self.monitored_pid = monitored_pid
        self.on_peer_lost = on_peer_lost

        self.server_socket = None
        self.client_socket = None
        self.last_heartbeat = time.time()
        self.startup_time = time.time()
        self.peer_ever_connected = False
        self.running = True
        self.lock = threading.Lock()

        self.server_thread = threading.Thread(
            target=self._run_server, daemon=True, name="ServerThread")
        self.client_thread = threading.Thread(
            target=self._run_client, daemon=True, name="ClientThread")
        self.checker_thread = threading.Thread(
            target=self._heartbeat_checker, daemon=True, name="CheckerThread")
        self.proc_monitor_thread = threading.Thread(
            target=self._local_process_monitor, daemon=True, name="ProcMonitorThread")

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
        self._close_sockets()

    def _close_sockets(self):
        with self.lock:
            for attr in ('client_socket', 'server_socket'):
                s = getattr(self, attr, None)
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)

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
                        log.info(f"对方已连接: {addr}")
                        threading.Thread(target=self._handle_incoming,
                                         args=(conn,), daemon=True).start()
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

    def _handle_incoming(self, conn):
        self.peer_ever_connected = True
        conn.settimeout(HEARTBEAT_INTERVAL + 2)
        while self.running:
            try:
                data = conn.recv(1024)
                if not data:
                    log.warning("对方关闭了连接")
                    break
                self.last_heartbeat = time.time()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    log.warning(f"接收数据错误: {e}")
                break
        try:
            conn.close()
        except Exception:
            pass

    def _run_client(self):
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((LOCALHOST, self.peer_port))
                log.info(f"已连接到对方: {LOCALHOST}:{self.peer_port}")
                self.peer_ever_connected = True
                # 连接成功后重置心跳时间，给对方一点时间建立反向连接
                self.last_heartbeat = time.time()
                with self.lock:
                    self.client_socket = sock
                while self.running:
                    try:
                        sock.sendall(HEARTBEAT_MSG)
                    except Exception as e:
                        log.warning(f"发送心跳失败: {e}")
                        break
                    time.sleep(HEARTBEAT_INTERVAL)
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                if self.running:
                    log.warning(f"连接对方失败 ({LOCALHOST}:{self.peer_port}): {e}")
                    log.info(f"{RECONNECT_INTERVAL}秒后重试...")
            finally:
                with self.lock:
                    self.client_socket = None
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                if self.running:
                    time.sleep(RECONNECT_INTERVAL)

    def _heartbeat_checker(self):
        # 启动宽限期：60秒内即使从未连接也不判定超时，给另一实例足够启动时间
        STARTUP_GRACE_PERIOD = 60
        while self.running:
            time.sleep(1)
            elapsed = time.time() - self.last_heartbeat
            since_startup = time.time() - self.startup_time
            if elapsed > HEARTBEAT_TIMEOUT:
                # 启动宽限期内且从未连接过对方：等待而非判定超时
                if since_startup < STARTUP_GRACE_PERIOD and not self.peer_ever_connected:
                    log.info(f"[{int(since_startup)}s] 等待对方实例启动中... (宽限期剩余 {STARTUP_GRACE_PERIOD - int(since_startup)}s)")
                    continue
                log.error(f"心跳超时！已 {elapsed:.0f} 秒未收到对方心跳")
                log.error("对方已掉线，正在结束本地 Minecraft 客户端...")
                self.on_peer_lost()
                self.running = False
                break

    def _local_process_monitor(self):
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            if not check_process_alive(self.monitored_pid):
                log.warning(f"本地 Minecraft 进程已退出 (PID: {self.monitored_pid})")
                log.warning("主动断开连接，通知对方...")
                self._close_sockets()
                self.running = False
                break


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
        log.info("全自动检测模式 - 无需任何手动操作")
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