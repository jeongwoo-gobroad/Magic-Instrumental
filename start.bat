@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installers\windows.ps1" -Mode Launch %*
set "LAUNCH_RESULT=%ERRORLEVEL%"
if not "%LAUNCH_RESULT%"=="0" if not "%MAGIC_NO_PAUSE%"=="1" pause
exit /b %LAUNCH_RESULT%
