#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全局报错日志模块
================
功能：
  - 创建 logs/ 目录下的带时间戳日志文件
  - 为 Python 日志系统添加文件 handler
  - 捕获全局未处理异常（sys.excepthook）
  - 捕获线程内未处理异常（threading.excepthook）
  - 自动清理过期日志（保留最近 N 个）
  - 兼容控制台同步输出
"""

import logging
import os
import sys
import threading
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# 北京时间时区
_CN_TZ = timezone(timedelta(hours=8))

# 脚本所在目录
_BASE_DIR = Path(__file__).parent.resolve()
_LOGS_DIR = _BASE_DIR / "logs"

# 全局 logger 名称
LOGGER_NAME = "afk_monitor"

# 日志格式
LOG_FORMAT = '[%(asctime)s] [%(levelname)s] [%(threadName)s] %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# 报错日志文件名前缀
ERROR_LOG_PREFIX = "error"

# 保留最近 N 个报错日志文件
MAX_LOG_FILES = 20

# ==================== 初始化 ====================
_initialized: bool = False
_log_file_path: Optional[Path] = None

# 保存原始 excepthook，避免多重包装
_original_excepthook = sys.excepthook
_original_thread_excepthook = getattr(threading, 'excepthook', None)
# 注意：Python 3.8 之前没有 threading.excepthook


def _ensure_logs_dir() -> Path:
    """确保 logs 目录存在"""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return _LOGS_DIR


def _cleanup_old_logs(max_files: int = MAX_LOG_FILES) -> None:
    """清理过期的报错日志文件，只保留最近 max_files 个"""
    try:
        pattern = f"{ERROR_LOG_PREFIX}_*.log"
        log_files = sorted(
            _LOGS_DIR.glob(pattern),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )
        for old_file in log_files[max_files:]:
            try:
                old_file.unlink()
            except OSError:
                pass
    except Exception:
        pass  # 清理失败不影响主流程


def _build_log_filename() -> str:
    """生成带时间戳的日志文件名"""
    timestamp = datetime.now(_CN_TZ).strftime("%Y%m%d_%H%M%S")
    return f"{ERROR_LOG_PREFIX}_{timestamp}.log"


def get_log_file_path() -> Optional[Path]:
    """获取当前报错日志文件路径"""
    return _log_file_path


def write_error_to_log(message: str, level: str = "ERROR") -> None:
    """
    手动向报错日志文件写入一条错误消息。
    用于记录被 try/except 处理但仍然导致脚本退出的错误。

    参数:
        message: 错误消息文本
        level: 日志级别 (ERROR/WARNING/CRITICAL/INFO)
    """
    try:
        now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_file_path, 'a', encoding='utf-8') as f:
            f.write(f"[{now}] [{level}] {message}\n")
    except Exception:
        pass  # 极端情况：连写入都失败就放弃


def _global_exception_handler(exc_type, exc_value, exc_tb) -> None:
    """
    全局未捕获异常处理。
    将完整的堆栈信息写入报错日志文件和 stderr。
    """
    if issubclass(exc_type, KeyboardInterrupt):
        # Ctrl+C 由 afk_monitor 的信号处理负责，此处仅委托原始 hook
        if _original_excepthook is not sys.__excepthook__:
            _original_excepthook(exc_type, exc_value, exc_tb)
        return

    # 构建格式化异常信息
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
    tb_text = "".join(tb_lines)

    # 输出到 stderr
    print(f"\n{'='*60}", file=sys.stderr)
    print("[ERROR] 未捕获的异常！详细信息如下：", file=sys.stderr)
    print(tb_text, file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    # 写入报错日志文件
    if _log_file_path:
        try:
            now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            with open(_log_file_path, 'a', encoding='utf-8') as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[{now}] [CRITICAL] 未捕获的异常\n")
                f.write(f"{'='*60}\n")
                f.write(tb_text)
                f.write(f"{'='*60}\n\n")
        except Exception:
            pass  # 极端兜底：连写文件都失败就不再处理

    # 在日志文件中额外记录一段进程/线程快照
    _dump_process_snapshot()

    # 调用原始 hook（如果有）
    if _original_excepthook is not sys.__excepthook__:
        try:
            _original_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass


def _thread_exception_handler(args) -> None:
    """
    线程内未捕获异常处理（Python 3.8+ threading.excepthook）。
    """
    exc_type, exc_value, exc_tb = args.exc_type, args.exc_value, args.exc_traceback

    tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
    tb_text = "".join(tb_lines)

    thread_name = args.thread.name if args.thread else "Unknown"

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"[ERROR] 线程内未捕获异常 (线程: {thread_name})：", file=sys.stderr)
    print(tb_text, file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    if _log_file_path:
        try:
            now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            with open(_log_file_path, 'a', encoding='utf-8') as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[{now}] [CRITICAL] 线程内未捕获异常 (线程: {thread_name})\n")
                f.write(f"{'='*60}\n")
                f.write(tb_text)
                f.write(f"{'='*60}\n\n")
        except Exception:
            pass

    _dump_process_snapshot()

    # 调用原始 hook
    if _original_thread_excepthook is not None:
        try:
            _original_thread_excepthook(args)
        except Exception:
            pass


def _dump_process_snapshot() -> None:
    """向报错日志写入进程/线程/系统快照，便于诊断"""
    if not _log_file_path:
        return
    try:
        import psutil
        now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_file_path, 'a', encoding='utf-8') as f:
            f.write(f"[{now}] [DEBUG] 系统快照:\n")
            # 当前进程信息
            proc = psutil.Process()
            f.write(f"  当前进程 PID: {proc.pid}, 名称: {proc.name()}\n")
            f.write(f"  CPU 使用率: {proc.cpu_percent(interval=0.1):.1f}%\n")
            mem = proc.memory_info()
            f.write(f"  内存使用: RSS={mem.rss / 1024 / 1024:.1f}MB, "
                    f"VMS={mem.vms / 1024 / 1024:.1f}MB\n")

            # 活跃线程
            threads = threading.enumerate()
            f.write(f"  活跃线程数: {len(threads)}\n")
            for t in threads:
                alive = "Alive" if t.is_alive() else "Stopped"
                daemon = "Daemon" if t.daemon else "NonDaemon"
                f.write(f"    - {t.name} ({alive}, {daemon})\n")

            # 系统整体内存
            try:
                sys_mem = psutil.virtual_memory()
                f.write(f"  系统内存: 总量={sys_mem.total / 1024 / 1024 / 1024:.1f}GB, "
                        f"可用={sys_mem.available / 1024 / 1024 / 1024:.1f}GB, "
                        f"使用率={sys_mem.percent:.1f}%\n")
            except Exception:
                f.write("  系统内存: 获取失败\n")

            f.write("\n")
    except ImportError:
        # psutil 不可用时的轻量快照
        try:
            now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            with open(_log_file_path, 'a', encoding='utf-8') as f:
                f.write(f"[{now}] [DEBUG] 系统快照 (psutil 不可用):\n")
                f.write(f"  Python 版本: {sys.version}\n")
                f.write(f"  平台: {sys.platform}\n")
                threads = threading.enumerate()
                f.write(f"  活跃线程数: {len(threads)}\n")
                for t in threads:
                    alive = "Alive" if t.is_alive() else "Stopped"
                    daemon = "Daemon" if t.daemon else "NonDaemon"
                    f.write(f"    - {t.name} ({alive}, {daemon})\n")
                f.write("\n")
        except Exception:
            pass
    except Exception:
        pass


def setup_error_logging() -> Path:
    """
    初始化全局报错日志系统。
    - 创建 logs/ 目录和时间戳命名的报错日志文件
    - 安装 sys.excepthook 和 threading.excepthook
    - 为 afk_monitor logger 添加文件 handler
    - 清理过期日志文件

    返回报错日志文件路径。
    """
    global _initialized, _log_file_path

    if _initialized:
        # 如果日志文件路径因某些原因丢失，重新创建
        if _log_file_path is None:
            _ensure_logs_dir()
            _log_file_path = _LOGS_DIR / _build_log_filename()
        return _log_file_path

    # 创建日志目录
    _ensure_logs_dir()

    # 生成日志文件名
    log_filename = _build_log_filename()
    _log_file_path = _LOGS_DIR / log_filename

    # 为 logger 添加文件 handler
    logger = logging.getLogger(LOGGER_NAME)
    try:
        file_handler = logging.FileHandler(
            _log_file_path, encoding='utf-8', mode='a'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
        )
        logger.addHandler(file_handler)
    except Exception:
        pass

    # 安装全局异常捕获
    sys.excepthook = _global_exception_handler

    # 安装线程异常捕获（Python 3.8+）
    if hasattr(threading, 'excepthook'):
        threading.excepthook = _thread_exception_handler

    # 清理过期日志
    _cleanup_old_logs()

    # 写入启动标记
    try:
        now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_file_path, 'a', encoding='utf-8') as f:
            f.write(f"{'='*60}\n")
            f.write(f"[{now}] [INFO] 报错日志系统已初始化\n")
            f.write(f"  Python 版本: {sys.version}\n")
            f.write(f"  平台: {sys.platform}\n")
            f.write(f"  工作目录: {Path.cwd()}\n")
            f.write(f"  脚本目录: {_BASE_DIR}\n")
            f.write(f"{'='*60}\n\n")
    except Exception:
        pass

    _initialized = True
    return _log_file_path


def shutdown_error_logging() -> None:
    """优雅关闭报错日志系统"""
    global _initialized, _log_file_path

    if _log_file_path:
        try:
            now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            with open(_log_file_path, 'a', encoding='utf-8') as f:
                f.write(f"\n[{now}] [INFO] 报错日志系统已关闭\n")
                f.write(f"{'='*60}\n")
        except Exception:
            pass

    # 恢复原始 hook
    sys.excepthook = _original_excepthook

    if _original_thread_excepthook is not None and hasattr(threading, 'excepthook'):
        threading.excepthook = _original_thread_excepthook

    _initialized = False
    _log_file_path = None