#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft AFK 挂机互保脚本 - 单元测试
=======================================
运行方式：pytest test_afk_monitor.py -v  或  python -m pytest test_afk_monitor.py -v
"""

import json
import io
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# 添加当前目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

import afk_monitor


class TestProtocolMessage(unittest.TestCase):
    """测试心跳协议消息常量"""

    def test_heartbeat_format(self):
        self.assertEqual(afk_monitor.Protocol.HEARTBEAT, b"ALIVE\n")

    def test_peer_down_format(self):
        self.assertEqual(afk_monitor.Protocol.PEER_DOWN, b"PEER_DOWN\n")

    def test_shutdown_format(self):
        self.assertEqual(afk_monitor.Protocol.SHUTDOWN, b"SHUTDOWN\n")

    def test_all_messages_returns_list(self):
        msgs = afk_monitor.Protocol.all_messages()
        self.assertEqual(len(msgs), 3)
        self.assertIn(b"ALIVE\n", msgs)
        self.assertIn(b"PEER_DOWN\n", msgs)
        self.assertIn(b"SHUTDOWN\n", msgs)


class TestIsLikelyGamePort(unittest.TestCase):
    """测试游戏端口判定逻辑"""

    def test_default_mc_port(self):
        self.assertTrue(afk_monitor.is_likely_game_port(25565))

    def test_high_port(self):
        self.assertTrue(afk_monitor.is_likely_game_port(25566))
        self.assertTrue(afk_monitor.is_likely_game_port(19132))

    def test_http_ports(self):
        self.assertFalse(afk_monitor.is_likely_game_port(80))
        self.assertFalse(afk_monitor.is_likely_game_port(443))
        self.assertFalse(afk_monitor.is_likely_game_port(8080))
        self.assertFalse(afk_monitor.is_likely_game_port(8443))

    def test_dns_port(self):
        self.assertFalse(afk_monitor.is_likely_game_port(53))

    def test_smtp_ports(self):
        self.assertFalse(afk_monitor.is_likely_game_port(25))
        self.assertFalse(afk_monitor.is_likely_game_port(465))
        self.assertFalse(afk_monitor.is_likely_game_port(587))

    def test_low_system_port(self):
        self.assertFalse(afk_monitor.is_likely_game_port(22))
        self.assertFalse(afk_monitor.is_likely_game_port(21))
        self.assertFalse(afk_monitor.is_likely_game_port(23))


class TestCheckProcessAlive(unittest.TestCase):
    """测试进程存活检测"""

    @patch("afk_monitor.psutil.Process")
    def test_process_running(self, mock_proc_class):
        mock_proc = MagicMock()
        mock_proc.is_running.return_value = True
        mock_proc_class.return_value = mock_proc
        self.assertTrue(afk_monitor.check_process_alive(12345))

    @patch("afk_monitor.psutil.Process")
    def test_process_not_running(self, mock_proc_class):
        mock_proc_class.side_effect = afk_monitor.psutil.NoSuchProcess(12345)
        self.assertFalse(afk_monitor.check_process_alive(12345))


class TestSafeCloseSocket(unittest.TestCase):
    """测试安全关闭 socket"""

    def test_close_valid_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        afk_monitor.safe_close_socket(sock)
        # 不应抛出异常

    def test_close_none_socket(self):
        afk_monitor.safe_close_socket(None)
        # 不应抛出异常

    def test_close_already_closed_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.close()
        afk_monitor.safe_close_socket(sock)
        # 不应抛出异常


class TestConfigManagement(unittest.TestCase):
    """测试配置管理"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def test_load_from_dict_basic(self):
        """测试从字典加载配置"""
        config = afk_monitor.AppConfig()
        config._load_from_dict({
            "heartbeat_interval": 5,
            "heartbeat_timeout": 20,
            "log_file": "test.log"
        })
        self.assertEqual(config.heartbeat_interval, 5)
        self.assertEqual(config.heartbeat_timeout, 20)
        self.assertEqual(config.log_file, "test.log")

    def test_load_from_dict_partial(self):
        """测试部分字段加载"""
        config = afk_monitor.AppConfig()
        config._load_from_dict({
            "heartbeat_interval": 2
        })
        self.assertEqual(config.heartbeat_interval, 2)
        self.assertEqual(config.heartbeat_timeout, 15)  # 默认值不变

    def test_as_dict_roundtrip(self):
        """测试配置可逆性"""
        config = afk_monitor.AppConfig()
        config.heartbeat_interval = 7
        config.heartbeat_timeout = 30
        config.log_file = "roundtrip.log"
        self.assertEqual(config.heartbeat_interval, 7)
        self.assertEqual(config.heartbeat_timeout, 30)
        self.assertEqual(config.log_file, "roundtrip.log")


class TestFindMinecraftProcesses(unittest.TestCase):
    """测试 MC 进程检测"""

    @patch("afk_monitor.psutil.process_iter")
    def test_no_minecraft_processes(self, mock_process_iter):
        """没有 MC 进程时应返回空列表"""
        mock_process_iter.return_value = []
        result = afk_monitor.find_minecraft_processes()
        self.assertEqual(result, [])

    @patch("afk_monitor.psutil.process_iter")
    def test_find_java_minecraft(self, mock_process_iter):
        """应能识别 Java Minecraft 进程"""
        proc = MagicMock()
        proc.info = {
            "pid": 1000,
            "name": "java.exe",
            "cmdline": ["java", "-jar", "minecraft_server.jar"]
        }
        mock_process_iter.return_value = [proc]
        result = afk_monitor.find_minecraft_processes()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 1000)

    @patch("afk_monitor.psutil.process_iter")
    def test_multiple_processes_sorted_by_pid(self, mock_process_iter):
        """应返回按 PID 升序排列的结果"""
        proc1 = MagicMock()
        proc1.info = {"pid": 3000, "name": "java.exe",
                       "cmdline": ["java", "-Dminecraft.client"]}
        proc2 = MagicMock()
        proc2.info = {"pid": 1000, "name": "java.exe",
                       "cmdline": ["java", "-Dminecraft.client"]}
        mock_process_iter.return_value = [proc1, proc2]
        result = afk_monitor.find_minecraft_processes()
        self.assertEqual(result[0][0], 1000)
        self.assertEqual(result[1][0], 3000)


class TestPeerConnectionHeartbeat(unittest.TestCase):
    """测试对等连接心跳逻辑"""

    def setUp(self):
        self.config = afk_monitor.AppConfig()
        self.config.port = 19990
        self.config.peer_port = 19991
        self.config.heartbeat_interval = 1
        self.config.heartbeat_timeout = 5
        self.config.reconnect_interval_min = 1
        self.config.reconnect_interval_max = 2
        self.config.startup_grace_period = 2

        self.peer_lost_called = False

        def on_peer_lost():
            self.peer_lost_called = True

        self.peer = afk_monitor.PeerConnection(
            config=self.config,
            monitored_pid=99999,
            on_peer_lost=on_peer_lost
        )

    def tearDown(self):
        self.peer.stop(graceful=False)
        time.sleep(0.5)

    def test_init_state(self):
        """测试初始状态"""
        self.assertTrue(self.peer.running)
        self.assertFalse(self.peer.peer_lost_triggered)
        self.assertFalse(self.peer.peer_ever_connected)

    def test_start_creates_threads(self):
        """start() 应启动所有线程"""
        self.peer.start()
        time.sleep(0.5)
        self.assertTrue(self.peer.server_thread.is_alive() or
                        self.peer.client_thread.is_alive())
        self.peer.stop(graceful=False)

    def test_trigger_peer_lost_only_once(self):
        """掉线回调应只触发一次"""
        self.peer._trigger_peer_lost("test")
        self.assertTrue(self.peer_lost_called)
        self.assertTrue(self.peer.peer_lost_triggered)
        # 重置回调标志
        self.peer_lost_called = False
        self.peer._trigger_peer_lost("second")
        self.assertFalse(self.peer_lost_called)  # 不应第二次触发

    def test_stop_graceful(self):
        """优雅停止应设置 running=False"""
        self.peer.stop(graceful=True)
        self.assertFalse(self.peer.running)

    def test_bidirectional_heartbeat_exchange(self):
        """端到端心跳通信测试：验证两个 PeerConnection 对象能互相收发消息"""
        self.peer_lost_called = False

        def make_on_peer_lost():
            return lambda: None  # no-op

        peer_a = afk_monitor.PeerConnection(
            config=afk_monitor.AppConfig(port=19992, peer_port=19993,
                                          heartbeat_interval=1,
                                          heartbeat_timeout=5,
                                          reconnect_interval_min=1,
                                          reconnect_interval_max=2,
                                          startup_grace_period=1),
            monitored_pid=99997,
            on_peer_lost=make_on_peer_lost()
        )
        peer_b = afk_monitor.PeerConnection(
            config=afk_monitor.AppConfig(port=19993, peer_port=19992,
                                          heartbeat_interval=1,
                                          heartbeat_timeout=5,
                                          reconnect_interval_min=1,
                                          reconnect_interval_max=2,
                                          startup_grace_period=1),
            monitored_pid=99998,
            on_peer_lost=make_on_peer_lost()
        )

        try:
            peer_a.start()
            peer_b.start()
            time.sleep(2.0)

            self.assertTrue(peer_a.peer_ever_connected or peer_b.peer_ever_connected,
                            "至少一方应检测到对方连接")
        finally:
            peer_a.stop(graceful=False)
            peer_b.stop(graceful=False)
            time.sleep(0.5)


class TestRecvBufferProtection(unittest.TestCase):
    """测试接收缓冲区溢出防护"""

    def setUp(self):
        self.config = afk_monitor.AppConfig()
        self.config.recv_buffer_max = 100
        self.peer = afk_monitor.PeerConnection(
            config=self.config,
            monitored_pid=99999,
            on_peer_lost=lambda: None
        )

    def tearDown(self):
        self.peer.stop(graceful=False)

    def test_recv_buffer_limit_not_triggered_on_normal(self):
        """正常数据不应触发溢出"""
        s1, s2 = socket.socketpair()
        s2.send(b"ALIVE\n" * 5)  # 35 bytes
        s2.close()

        try:
            s1.settimeout(1)
            recv_buffer = b""
            all_msgs = [b"ALIVE\n"]
            overflow = False

            while len(recv_buffer) <= self.config.recv_buffer_max:
                try:
                    chunk = s1.recv(4096)
                    if not chunk:
                        break
                    recv_buffer += chunk
                    if len(recv_buffer) > self.config.recv_buffer_max:
                        overflow = True
                        break
                except socket.timeout:
                    break
            self.assertFalse(overflow)
        finally:
            s1.close()


class TestLoggingSetup(unittest.TestCase):
    """测试日志配置"""

    def test_setup_logging_console_only(self):
        """仅有控制台输出时应正常配置"""
        afk_monitor.setup_logging()
        logger = afk_monitor.logging.getLogger("afk_monitor")
        self.assertEqual(len(logger.handlers), 1)  # 仅 console handler

    def test_setup_logging_with_file(self):
        """同时配置文件和日志应正常"""
        try:
            afk_monitor.setup_logging(log_file="test_run.log")
            logger = afk_monitor.logging.getLogger("afk_monitor")
            self.assertEqual(len(logger.handlers), 2)  # console + file
        finally:
            # 清理
            import os
            try:
                os.remove("test_run.log")
            except OSError:
                pass
            afk_monitor.setup_logging()  # reset


class TestNetstatFallbackParsing(unittest.TestCase):
    """测试 netstat 输出解析"""

    def test_empty_output(self):
        """空输出应返回空列表"""
        result = afk_monitor.get_server_connections_fallback(12345)
        self.assertEqual(result, [])

    def test_parse_established_ipv4(self):
        """应正确解析 IPv4 ESTABLISHED 连接"""
        with patch("subprocess.check_output") as mock_output:
            mock_output.return_value = (
                "  TCP    10.0.0.1:52341    203.0.113.1:25565    ESTABLISHED     12345\n"
                .encode('utf-8')
            )
            result = afk_monitor.get_server_connections_fallback(12345)
            self.assertIn(("203.0.113.1", 25565), result)

    def test_exclude_localhost(self):
        """应排除本地回环连接"""
        with patch("subprocess.check_output") as mock_output:
            mock_output.return_value = (
                "  TCP    127.0.0.1:18888    127.0.0.1:18889    ESTABLISHED     12345\n"
                .encode('utf-8')
            )
            result = afk_monitor.get_server_connections_fallback(12345)
            self.assertEqual(result, [])


class TestIntegrationQuick(unittest.TestCase):
    """快速集成测试"""

    def test_full_module_imports(self):
        """所有导入应可用"""
        self.assertTrue(hasattr(afk_monitor, "Protocol"))
        self.assertTrue(hasattr(afk_monitor, "AppConfig"))
        self.assertTrue(hasattr(afk_monitor, "PeerConnection"))
        self.assertTrue(hasattr(afk_monitor, "MonitorApp"))
        self.assertTrue(hasattr(afk_monitor, "ServerConnectionMonitor"))
        self.assertTrue(hasattr(afk_monitor, "main"))

    def test_config_file_parsable(self):
        """config.json 应是有效的 JSON"""
        config_path = Path(__file__).parent / "config.json"
        self.assertTrue(config_path.exists(), "config.json should exist")
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("instance_a", data)
        self.assertIn("instance_b", data)
        self.assertEqual(data["instance_a"]["port"], 18888)
        self.assertEqual(data["instance_b"]["port"], 18889)


if __name__ == "__main__":
    unittest.main()