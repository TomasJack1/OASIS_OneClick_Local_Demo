@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title OASIS Multi-Agent Social Simulation

echo ========================================================
echo OASIS 多智能体社交网络仿真网页（本机独立运行版）
echo ========================================================
echo.
echo First run installs Python 3.11 and project dependencies.
echo This may require downloading several hundred MB or more.
echo.
if not exist ".dependencies_ready" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
    if errorlevel 1 (
        echo [ERROR] Setup failed. Read the error lines above.
        pause
        exit /b 1
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Missing local Python. Delete .dependencies_ready and retry.
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] Missing package configuration file.
    pause
    exit /b 1
)

echo [START] Local webpage: http://127.0.0.1:8501
echo [INFO] Leave this command window open while using OASIS.
echo.
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
if errorlevel 1 echo [ERROR] Streamlit exited with an error.
pause
