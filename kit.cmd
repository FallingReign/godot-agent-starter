@echo off
setlocal
set "KIT_ROOT=%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"

if exist "%KIT_ROOT%.agent-kit\launcher.py" goto use_managed_launcher
if exist "%KIT_ROOT%.agent-kit" goto managed_launcher_missing
set "KIT_ENTRY=%KIT_ROOT%tools\managed_launcher.py"
goto launcher_selected

:use_managed_launcher
set "KIT_ENTRY=%KIT_ROOT%.agent-kit\launcher.py"
goto launcher_selected

:managed_launcher_missing
echo kit: the managed kit launcher is missing; restore or roll back the interrupted kit change. 1>&2
exit /b 3

:launcher_selected
if exist "%KIT_ENTRY%" goto launcher_exists
echo kit: the kit launcher is missing. 1>&2
exit /b 3

:launcher_exists

if defined KIT_PYTHON goto use_kit_python

where py >nul 2>&1
if not errorlevel 1 goto use_py
where python3 >nul 2>&1
if not errorlevel 1 goto use_python3
where python >nul 2>&1
if not errorlevel 1 goto use_python

echo kit: its internal runtime is unavailable; Python 3.10 or newer is required. 1>&2
exit /b 3

:use_kit_python
for %%I in ("%KIT_PYTHON%") do set "KIT_PYTHON_RESOLVED=%%~fI"
if /I "%KIT_PYTHON%"=="%KIT_PYTHON_RESOLVED%" goto kit_python_absolute
echo kit: KIT_PYTHON must name one absolute interpreter file. 1>&2
exit /b 3

:kit_python_absolute
if exist "%KIT_PYTHON%" goto kit_python_exists
echo kit: KIT_PYTHON does not name an existing interpreter file. 1>&2
exit /b 3

:kit_python_exists
for %%I in ("%KIT_PYTHON%") do set "KIT_PYTHON_ATTRIBUTES=%%~aI"
if /I not "%KIT_PYTHON_ATTRIBUTES:~0,1%"=="d" goto kit_python_file
echo kit: KIT_PYTHON must name a file, not a directory. 1>&2
exit /b 3

:kit_python_file
"%KIT_PYTHON%" "%KIT_ENTRY%" %*
exit /b %errorlevel%

:use_py
py -3 "%KIT_ENTRY%" %*
exit /b %errorlevel%

:use_python3
python3 "%KIT_ENTRY%" %*
exit /b %errorlevel%

:use_python
python "%KIT_ENTRY%" %*
exit /b %errorlevel%
