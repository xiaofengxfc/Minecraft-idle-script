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
echo 选择模式:
echo   [1] 自动检测 Minecraft 进程（推荐）
echo   [2] 手动输入进程 PID
echo.
set /p MODE="请选择模式 (1/2, 默认1): "
if "%MODE%"=="" set MODE=1
if "%MODE%"=="2" goto manual

:auto
echo.
echo 正在启动监控... (监听端口: 18888, 对方端口: 18889)
echo 模式: 自动检测 Minecraft 进程
echo.
python afk_monitor.py --port 18888 --peer-port 18889 --auto
goto end

:manual
echo.
set /p MCPID="请输入 Minecraft 客户端 A 的进程 PID: "
echo.
echo 正在启动监控... (监听端口: 18888, 对方端口: 18889)
echo 监控 PID: %MCPID%
echo.
python afk_monitor.py --port 18888 --peer-port 18889 --pid %MCPID%
goto end

:end
pause
