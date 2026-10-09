@echo off
chcp 65001 >nul
title 幸运大转盘 - 服务运行中
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    echo 正在启动服务...
    python app.py
    goto end
)

where py >nul 2>nul
if %errorlevel%==0 (
    echo 正在启动服务...
    py -3 app.py
    goto end
)

echo ============================================================
echo  没有检测到 Python。
echo  请先到 https://www.python.org/downloads/ 下载安装 Python 3.8+，
echo  安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
echo ============================================================

:end
echo.
echo 服务已停止，按任意键关闭窗口。
pause >nul
