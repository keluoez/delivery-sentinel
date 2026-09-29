@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   交期哨兵 Delivery Sentinel  starting...
echo   http://127.0.0.1:8765
echo ============================================
start "" http://127.0.0.1:8765
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
