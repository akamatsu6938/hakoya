@echo off
chcp 65001 >nul
rem HAKOYA を「見本用」で起動する（審査・動画用）。データは data\sample\（見本の家だけ）。口は 8767 番。
rem 初めて起動したときに、同梱の見本の家（samples\sample_house）か、自社用の data\ の組み立て済みの「デモ用」を写します。
rem 止めるときはこの画面で Ctrl+C。
cd /d "%~dp0"
set PYTHONUTF8=1
set HAKOYA_MODE=sample
".venv\Scripts\python.exe" -m app.server
pause
