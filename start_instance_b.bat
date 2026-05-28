@echo off
chcp 65001 >nul
title AFK监控 - 实例B (端口18889 - 对方18888)
echo ========================================
echo Minecraft AFK 挂机互保脚本 - 实例 B
echo ========================================
echo.
echo 请先启动两个 Minecraft 客户端，然后执行此脚本。
echo 启动后请勿关闭本窗口，否则对方检测到掉线会结束 MC 进程。
echo.
echo 正在自动检测 Minecraft 进程...
echo 实例B 将绑定第 2 个 MC 进程 (按PID升序)
echo 监听端口: 18889, 对方端口: 18888
echo.
python afk_monitor.py --port 18889 --peer-port 18888 --auto --auto-index 1
pause