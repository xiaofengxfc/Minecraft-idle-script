#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft AFK 挂机互保脚本
============================
功能：两个 Minecraft 客户端在同一台机器上运行时，通过本地 TCP 互联互相检测在线状态。
      一方掉线（进程退出或网络断开），另一方自动结束自己的 Minecraft 客户端进程。

使用方式：
    python afk_monitor.py --port 18888 --peer-port 18889 --pid <MC进程PID>
    python afk_monitor.py --port 18889 --peer-port 18888 --pid <MC进程PID>

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
from datetime import datetime
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
HEARTBEAT_INTERVAL = 3        # 心跳间隔（秒）
HEARTBEAT_TIMEOUT = 15        # 心跳超时时间（秒），超时认为对方掉线
RECONNECT_INTERVAL = 5        # 连接重试间隔（秒）
LOCALHOST = "127.0.0.1"       # 仅监听本地回环地址，保证纯本地通信

HEARTBEAT_MSG = b"ALIVE\n"    # 心跳消息

# ==================== Minecraft 进程自动检测 ====================
def find_minecraft_processes() -> List[Tuple[int, str, str]]:
    """
    扫描系统中所有正在运行的 Minecraft Java 进程。
    
    Returns:
        List of (pid, process_name, command_line) tuples.
        Returns empty list if none found.
    匹配规则：
        - 进程名包含 javaw.exe / java.exe / minecraft
        - 命令行参数包含 minecraft 或 forge/fabric/optifine 等启动器特征
    """
    minecraft_processes = []
    
    # Minecraft 相关的命令行特征关键字
    mc_keywords = [
        'minecraft', 'forge', 'fabric', 'nide8auth', 'authlib-injector',
        'launcher', 'LaunchClient', '-Dminecraft', 'lwjgl',
        'tlauncher', 'hmcl', 'pcl', 'bakaxl', 'plaincraft',
        'launchwrapper'
    ]
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            info = proc.info
            name = info.get('name', '').lower() if info.get('name') else ''
            pid = info.get('pid')
            cmdline = ' '.join(info.get('cmdline', [])) if info.get('cmdline') else ''
            cmdline_lower = cmdline.lower()
            
            # 检查进程名特征
            is_java_process = (
                'java' in name or 
                'javaw' in name or 
                'minecraft' in name
            )
            
            if not is_java_process:
                continue
            
            # 检查命令行是否包含 Minecraft 相关特征
            is_minecraft = any(keyword.lower() in cmdline_lower for keyword in mc_keywords)
            
            if is_minecraft:
                # 尝试提取更友好的描述
                display_name = name
                if 'minecraft' in cmdline_lower:
                    display_name = f"{name} [Minecraft]"
                elif any(k in cmdline_lower for k in ['forge', 'fabric']):
                    display_name = f"{name} [Modded MC]"
                else:
                    display_name = f"{name} [MC Launcher]"
                
                minecraft_processes.append((pid, display_name, cmdline))
                
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    return minecraft_processes


def interactive_select_minecraft_process(
    processes: List[Tuple[int, str, str]],
    allow_multi: bool = False,
    default_pids: Optional[List[int]] = None
) -> Optional[List[int]]:
    """
    交互式选择一个或多个 Minecraft 进程。
    
    Args:
        processes: find_minecraft_processes() 返回的进程列表
        allow_multi: 是否允许多选（用于启动两个实例时）
        default_pids: 如果提供了默认 PID 列表，且它们都在 processes 中，自动选择
        
    Returns:
        选中的 PID 列表，如果用户取消则返回 None
    """
    if not processes:
        return None
    
    # 如果有默认 PID 且它们在进程列表中，自动选择
    if default_pids:
        available_pids = {p[0] for p in processes}
        matched_pids = [pid for pid in default_pids if pid in available_pids]
        if matched_pids:
            log.info(f"自动匹配到指定的 Minecraft 进程 PID: {matched_pids}")
            return matched_pids
    
    print("\n" + "=" * 60)
    print("检测到以下 Minecraft 进程:")
    print("=" * 60)
    for i, (pid, name, cmdline) in enumerate(processes, 1):
        # 截断命令行以保持可读性
        cmd_display = cmdline[:120] + "..." if len(cmdline) > 120 else cmdline
        print(f"  [{i}] PID: {pid:<8} | {name}")
        print(f"      命令行: {cmd_display}")
        print()
    
    if allow_multi:
        print(f"选项: 输入编号选择 (如 '1' '2' 各选一个)，或输入 'all' 选择全部")
        print(f"      输入 'q' 退出")
    else:
        print(f"选项: 输入编号选择 (1-{len(processes)})，或输入 'q' 退出")
    
    while True:
        try:
            choice = input("\n请选择: ").strip().lower()
            
            if choice == 'q':
                return None
            
            if choice == 'all' and allow_multi:
                return [p[0] for p in processes]
            
            # 解析编号
            indices = [int(x.strip()) for x in choice.replace(',', ' ').split()]
            
            if not indices:
                print("输入无效，请重新输入")
                continue
            
            selected_pids = []
            for idx in indices:
                if 1 <= idx <= len(processes):
                    selected_pids.append(processes[idx - 1][0])
                else:
                    print(f"编号 {idx} 超出范围 (1-{len(processes)})")
                    break
            else:
                if selected_pids:
                    return selected_pids
                    
        except (ValueError, KeyboardInterrupt):
            if isinstance(choice, KeyboardInterrupt):
                return None
            print("输入无效，请输入数字编号")
    
    return None


# ==================== 进程管理 ====================
def check_process_alive(pid):
    """检查指定 PID 的进程是否存活"""
    try:
        proc = psutil.Process(pid)
        return proc.is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process(pid):
    """强制终止指定 PID 的进程"""
    try:
        proc = psutil.Process(pid)
        proc_name = proc.name()
        log.warning(f"正在终止进程: {proc_name} (PID: {pid})")
        proc.terminate()
        # 等待 3 秒让进程优雅退出
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
        # 回退方案：使用 Windows taskkill
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=5
            )
            log.info(f"通过 taskkill 终止进程: PID {pid}")
            return True
        except Exception as e2:
            log.error(f"taskkill 也失败: {e2}")
            return False


# ==================== 对等连接管理器 ====================
class PeerConnection:
    """
    管理与对等脚本的 TCP 连接
    同时作为服务端（监听对方连接）和客户端（主动连接对方）
    使用心跳机制检测对方是否在线
    """

    def __init__(self, port, peer_port, monitored_pid, on_peer_lost):
        """
        Args:
            port: 本实例监听的端口
            peer_port: 对方实例监听的端口
            monitored_pid: 要监控的 Minecraft 进程 PID
            on_peer_lost: 检测到对方掉线时的回调函数
        """
        self.port = port
        self.peer_port = peer_port
        self.monitored_pid = monitored_pid
        self.on_peer_lost = on_peer_lost

        self.server_socket = None
        self.client_socket = None
        self.last_heartbeat = time.time()
        self.running = True
        self.lock = threading.Lock()

        # 启动服务端线程
        self.server_thread = threading.Thread(
            target=self._run_server,
            daemon=True,
            name="ServerThread"
        )

        # 启动客户端线程
        self.client_thread = threading.Thread(
            target=self._run_client,
            daemon=True,
            name="ClientThread"
        )

        # 启动心跳检查线程
        self.checker_thread = threading.Thread(
            target=self._heartbeat_checker,
            daemon=True,
            name="CheckerThread"
        )

        # 启动本地进程监控线程
        self.proc_monitor_thread = threading.Thread(
            target=self._local_process_monitor,
            daemon=True,
            name="ProcMonitorThread"
        )

    def start(self):
        """启动所有线程"""
        log.info(f"本实例监听端口: {self.port}")
        log.info(f"对方实例端口: {self.peer_port}")
        log.info(f"监控进程 PID: {self.monitored_pid}")

        self.server_thread.start()
        self.client_thread.start()
        self.checker_thread.start()
        self.proc_monitor_thread.start()

    def stop(self):
        """停止所有连接"""
        self.running = False
        self._close_sockets()

    def _close_sockets(self):
        """关闭所有 socket"""
        with self.lock:
            if self.client_socket:
                try:
                    self.client_socket.close()
                except Exception:
                    pass
                self.client_socket = None
            if self.server_socket:
                try:
                    self.server_socket.close()
                except Exception:
                    pass
                self.server_socket = None

    def send_heartbeat(self):
        """通过客户端 socket 发送心跳"""
        with self.lock:
            sock = self.client_socket
        if sock:
            try:
                sock.sendall(HEARTBEAT_MSG)
            except Exception:
                pass

    # ==================== 服务端：接受对方连接 ====================
    def _run_server(self):
        """服务端线程：监听并接受对方的 TCP 连接"""
        while self.running:
            try:
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind((LOCALHOST, self.port))
                self.server_socket.listen(1)
                self.server_socket.settimeout(5)  # 5秒超时，便于检查 running 状态
                log.info(f"服务端已启动，等待对方连接: {LOCALHOST}:{self.port}")

                while self.running:
                    try:
                        conn, addr = self.server_socket.accept()
                        log.info(f"对方已连接: {addr}")
                        # 在一个线程中处理这个连接的数据接收
                        t = threading.Thread(
                            target=self._handle_incoming,
                            args=(conn,),
                            daemon=True
                        )
                        t.start()
                    except socket.timeout:
                        continue
                    except Exception as e:
                        if self.running:
                            log.error(f"accept 错误: {e}")
                        break
            except OSError as e:
                if self.running:
                    log.error(f"服务端启动失败 (端口 {self.port} 可能被占用): {e}")
                    log.info(f"5秒后重试...")
                    time.sleep(5)
            finally:
                if self.server_socket:
                    try:
                        self.server_socket.close()
                    except Exception:
                        pass
                    self.server_socket = None

    def _handle_incoming(self, conn):
        """处理对方发来的数据（心跳消息）"""
        conn.settimeout(HEARTBEAT_INTERVAL + 2)
        while self.running:
            try:
                data = conn.recv(1024)
                if not data:
                    log.warning("对方关闭了连接")
                    break
                # 收到心跳，更新时间戳
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

    # ==================== 客户端：主动连接对方 ====================
    def _run_client(self):
        """客户端线程：主动连接对方并定期发送心跳"""
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((LOCALHOST, self.peer_port))
                log.info(f"已连接到对方: {LOCALHOST}:{self.peer_port}")

                with self.lock:
                    self.client_socket = sock

                # 连接成功后定期发送心跳
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

    # ==================== 心跳超时检查 ====================
    def _heartbeat_checker(self):
        """检查心跳是否超时"""
        while self.running:
            time.sleep(1)
            elapsed = time.time() - self.last_heartbeat
            if elapsed > HEARTBEAT_TIMEOUT:
                log.error(f"心跳超时！已 {elapsed:.0f} 秒未收到对方心跳")
                log.error("对方已掉线，正在结束本地 Minecraft 客户端...")
                self.on_peer_lost()
                self.running = False
                break

    # ==================== 本地进程监控 ====================
    def _local_process_monitor(self):
        """
        监控本地 Minecraft 进程是否存活。
        如果本地 MC 进程退出，主动通知对方（关闭连接），
        对方的心跳检测会触发超时并结束自己的 MC 进程。
        """
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
        description="Minecraft AFK 挂机互保脚本 - 两个客户端互相检测在线状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  实例A: python afk_monitor.py --port 18888 --peer-port 18889 --pid 12345
  实例B: python afk_monitor.py --port 18889 --peer-port 18888 --pid 67890

注意: 两个实例需要使用不同的端口，且必须互相指定对方的端口。
        """
    )
    parser.add_argument(
        "--port", type=int, required=True,
        help="本实例监听的端口号（两个实例必须不同）"
    )
    parser.add_argument(
        "--peer-port", type=int, required=True,
        help="对方实例监听的端口号"
    )
    parser.add_argument(
        "--pid", type=int, required=False, default=None,
        help="要监控的 Minecraft 客户端进程 PID（与 --auto 互斥，但可同时指定用于自动匹配）"
    )
    parser.add_argument(
        "--auto", action="store_true", default=False,
        help="自动检测 Minecraft 进程（无需手动指定 PID）"
    )
    parser.add_argument(
        "--list", action="store_true", default=False,
        help="列出所有检测到的 Minecraft 进程后退出"
    )
    parser.add_argument(
        "--heartbeat-interval", type=int, default=HEARTBEAT_INTERVAL,
        help=f"心跳间隔秒数（默认: {HEARTBEAT_INTERVAL}）"
    )
    parser.add_argument(
        "--heartbeat-timeout", type=int, default=HEARTBEAT_TIMEOUT,
        help=f"心跳超时秒数（默认: {HEARTBEAT_TIMEOUT}）"
    )

    args = parser.parse_args()

    # 覆盖模块级全局配置，让 PeerConnection 的方法使用命令行指定值
    HEARTBEAT_INTERVAL = args.heartbeat_interval
    HEARTBEAT_TIMEOUT = args.heartbeat_timeout

    # ==================== PID 处理逻辑 ====================
    target_pid = args.pid
    
    # --list 模式：列出所有 Minecraft 进程后退出
    if args.list:
        processes = find_minecraft_processes()
        if not processes:
            print("未检测到任何 Minecraft 进程。")
        else:
            print("\n" + "=" * 60)
            print(f"检测到 {len(processes)} 个 Minecraft 进程:")
            print("=" * 60)
            for pid, name, cmdline in processes:
                cmd_display = cmdline[:150] + "..." if len(cmdline) > 150 else cmdline
                print(f"  PID: {pid:<8} | {name}")
                print(f"  命令行: {cmd_display}")
                print()
        sys.exit(0)
    
    # 自动检测模式
    if args.auto or target_pid is None:
        log.info("启用自动检测模式，正在扫描 Minecraft 进程...")
        processes = find_minecraft_processes()
        
        if not processes:
            log.error("未检测到任何 Minecraft 进程！")
            log.error("请确保 Minecraft 客户端已启动，或使用 --pid 手动指定 PID。")
            log.error("提示: 可使用 --list 查看所有已检测到的进程。")
            sys.exit(1)
        
        log.info(f"检测到 {len(processes)} 个 Minecraft 进程")
        
        # 如果用户同时指定了 --pid，尝试自动匹配
        selected_pids = None
        if target_pid is not None:
            selected_pids = interactive_select_minecraft_process(
                processes, allow_multi=False, default_pids=[target_pid]
            )
        else:
            # 纯自动模式：交互式选择（单个进程）
            selected_pids = interactive_select_minecraft_process(
                processes, allow_multi=False
            )
        
        if not selected_pids:
            log.error("未选择任何进程，退出。")
            sys.exit(1)
        
        target_pid = selected_pids[0]
    
    # 验证 PID 是否有效
    if not check_process_alive(target_pid):
        log.error(f"指定的进程 PID {target_pid} 不存在或无法访问！")
        log.error("请确认 PID 是否正确，进程是否正在运行。")
        log.error("提示: 可使用 --list 查看当前所有 Minecraft 进程。")
        sys.exit(1)

    proc = psutil.Process(target_pid)
    log.info(f"监控进程: {proc.name()} (PID: {target_pid})")
    log.info(f"心跳间隔: {HEARTBEAT_INTERVAL}s, 超时: {HEARTBEAT_TIMEOUT}s")

    # ==================== 掉线处理回调 ====================
    def on_peer_lost():
        """对方掉线时的处理：终止本地 Minecraft 进程"""
        log.warning("=" * 50)
        log.warning("检测到对方客户端掉线！")
        log.warning(f"正在结束本地 Minecraft 进程 (PID: {target_pid}) ...")
        log.warning("=" * 50)
        kill_process(target_pid)
        log.info("本地 Minecraft 进程已结束，脚本即将退出。")
        os._exit(0)  # 强制退出，不等待线程

    # ==================== 创建并启动对等连接 ====================
    peer = PeerConnection(
        port=args.port,
        peer_port=args.peer_port,
        monitored_pid=target_pid,
        on_peer_lost=on_peer_lost
    )

    # 注册信号处理（Ctrl+C 优雅退出）
    def signal_handler(sig, frame):
        log.info("\n收到退出信号，正在关闭...")
        peer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    peer.start()

    # 主线程保持运行，直到 running 变为 False
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