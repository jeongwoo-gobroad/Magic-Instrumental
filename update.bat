@echo off
rem SPDX-License-Identifier: GPL-3.0-only
(
 powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installers\update-windows.ps1"
 exit /b
)
