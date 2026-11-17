@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installers\windows.ps1" -Mode Install %*
set "INSTALL_RESULT=%ERRORLEVEL%"
if not "%MAGIC_NO_PAUSE%"=="1" pause
exit /b %INSTALL_RESULT%
