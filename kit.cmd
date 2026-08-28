@echo off
setlocal
set "KIT_ROOT=%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"

where py >nul 2>&1
if not errorlevel 1 goto use_py
where python3 >nul 2>&1
if not errorlevel 1 goto use_python3
where python >nul 2>&1
if not errorlevel 1 goto use_python

echo kit: its internal runtime is unavailable; Python 3.10 or newer is required. 1>&2
exit /b 3

:use_py
py -3 "%KIT_ROOT%kit.py" %*
exit /b %errorlevel%

:use_python3
python3 "%KIT_ROOT%kit.py" %*
exit /b %errorlevel%

:use_python
python "%KIT_ROOT%kit.py" %*
exit /b %errorlevel%
