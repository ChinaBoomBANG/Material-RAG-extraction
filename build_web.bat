@echo off
cd /d "%~dp0"
title MaterialRAG - build web UI
echo [1/1] Building frontend (Vite + Vue3 + Tailwind)...
echo.
"%~dp0my_venv\Scripts\python.exe" "%~dp0scripts\build_web.py" %*
echo.
if errorlevel 1 (
  echo BUILD FAILED - see messages above.
) else (
  echo BUILD OK - double-click start_web.vbs and open http://127.0.0.1:8848/
)
pause
