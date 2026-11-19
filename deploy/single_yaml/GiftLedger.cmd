@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0windows-runtime\python.exe" goto portable
if exist "%~dp0runtime\python.exe" goto legacy_runtime
where py.exe >nul 2>nul
if not errorlevel 1 goto py_launcher
where python.exe >nul 2>nul
if not errorlevel 1 goto python
echo Python 3.9+ is required. Install Python for Windows or place the GiftLedger
echo portable Python folder named windows-runtime next to this launcher.
set "result=1"
goto finish
:portable
"%~dp0windows-runtime\python.exe" -E -s -B -X utf8 "%~dp0windows_site.py" %*
goto done
:legacy_runtime
"%~dp0runtime\python.exe" -E -s -B -X utf8 "%~dp0windows_site.py" %*
goto done
:py_launcher
py.exe -3 -E -s -B -X utf8 "%~dp0windows_site.py" %*
goto done
:python
python.exe -E -s -B -X utf8 "%~dp0windows_site.py" %*
:done
set "result=%errorlevel%"
:finish
if "%~1"=="" pause
exit /b %result%
