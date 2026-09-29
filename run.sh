#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "交期哨兵 Delivery Sentinel → http://127.0.0.1:8765"
PYTHONIOENCODING=utf-8 python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
