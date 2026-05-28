@echo off
chcp 65001 >nul
title AFK监控 - 实例A (端口18888 - 对方18889)
echo ========================================
echo Minecraft AFK 挂机互保脚本 - 实例 A
echo ========================================
echo.
echo 请先启动两个 Minecraft 客户端，然后执行此脚本。
echo 启动后请勿关闭本窗口，否则对方检测到掉线会结束 MC 进程。
echo.
echo 正在自动检测 Minecraft 进程...
echo 实例A 将绑定第 1 个 MC 进程 (按PID升序)
echo 监听端口: 18888, 对方端口: 18889
echo.
python afk_monitor.py --port 18888 --peer-port 18889 --auto --auto-index 0
pause