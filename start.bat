@echo off
chcp 65001 >nul
rem HAKOYA を起動する（開発中）。ブラウザが自動で開きます。止めるときはこの画面で Ctrl+C。
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m app.server
pause
