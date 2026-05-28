@echo off
chcp 65001 >nul

:: 检查 Python 是否可用
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 并添加到 PATH 环境变量。
    pause
    exit /b 1
)

title AFK监控 - 实例A (端口18888 - 对方18889)
echo ========================================
echo Minecraft AFK 挂机互保脚本 - 实例 A
echo ========================================
echo.
echo 请先启动两个 Minecraft 客户端，然后执行此脚本。
echo 启动后请勿关闭本窗口，否则对方检测到掉线会结束 MC 进程。
echo.
echo 正在从 config.json 加载实例 A 配置...
echo.
python afk_monitor.py --instance a
pause