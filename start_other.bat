@echo off
chcp 65001 >nul
rem HAKOYA を「他社用」で起動する（開発中）。データは data\other\（最初は空。自社用の data\ とは別）。口は 8766 番。
rem AI は API キーと AI なしだけ。ブラウザが自動で開きます。止めるときはこの画面で Ctrl+C。
cd /d "%~dp0"
set PYTHONUTF8=1
set HAKOYA_MODE=other
".venv\Scripts\python.exe" -m app.server
pause
