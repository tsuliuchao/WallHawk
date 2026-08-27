@echo off
cd /d "%~dp0"
set UV_CACHE_DIR=%~dp0.uv-cache
if not exist venv ( uv venv venv )
uv pip install --python venv\Scripts\python.exe -r requirements.txt
echo >> 启动盯盘面板 http://localhost:8050
venv\Scripts\python.exe app.py
pause
