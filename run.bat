@echo off
cd /d "%~dp0"
set UV_CACHE_DIR=%~dp0.uv-cache

rem 可选配置文件：%USERPROFILE%\.config\wallhawk.env（含 PUSHPLUS_TOKEN 等环境变量）
if exist "%USERPROFILE%\.config\wallhawk.env" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%USERPROFILE%\.config\wallhawk.env") do set "%%a=%%b"
)

if not exist venv ( uv venv venv )
uv pip install --python venv\Scripts\python.exe -r requirements.txt
echo >> 启动盯盘助手 http://localhost:8050
venv\Scripts\python.exe app.py
pause
