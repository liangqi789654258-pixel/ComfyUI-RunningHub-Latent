@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  RH Image Studio - one-click installer
rem  Reuses the Python that ships with ComfyUI (no extra env).
rem
rem  Usage:
rem    install.bat                                  (auto-detect)
rem    install.bat "D:\ComfyUI_windows_portable\python_embeded\python.exe"
rem    install.bat "D:\ComfyUI\.venv\Scripts\python.exe"
rem ============================================================

set "PY=%~1"

if "%PY%"=="" call :detect_py
if "%PY%"=="" (
  echo.
  echo [X] ComfyUI python not found automatically.
  echo     Please run again with the path to your ComfyUI python, e.g.
  echo.
  echo     install.bat "D:\ComfyUI_windows_portable\python_embeded\python.exe"
  echo     install.bat "C:\ComfyUI\.venv\Scripts\python.exe"
  echo.
  pause
  exit /b 1
)

echo [1/3] Using python: %PY%
"%PY%" -c "import torch" 2>NUL
if errorlevel 1 (
  echo [X] torch not found in this python. This installer expects an
  echo     existing ComfyUI installation ^(its python already has torch^).
  pause
  exit /b 1
)

echo [2/3] Installing GUI dependencies (PySide6, pywin32) ...
"%PY%" -m pip install --quiet PySide6 pywin32
if errorlevel 1 (
  echo [X] pip install failed. Check your network / proxy and retry.
  pause
  exit /b 1
)

echo [3/3] Verifying ...
"%PY%" -c "import PySide6, torch; print('  PySide6', PySide6.__version__, '| torch', torch.__version__)"
if errorlevel 1 (
  echo [X] Verification failed.
  pause
  exit /b 1
)

echo.
echo ================================================
echo  Install OK!
echo  Launch:   pythonw main.py   (in this folder)
echo  A desktop shortcut is created automatically
echo  on first launch. API Key goes to: Settings.
echo ================================================
echo.
set "PW=%PY:python.exe=pythonw.exe%"
if exist "%PW%" (
  start "" "%PW%" "%~dp0main.py"
  echo App is starting...
) else (
  start "" "%PY%" "%~dp0main.py"
)
pause
exit /b 0

:detect_py
rem Try common ComfyUI layouts relative to this folder and drive roots.
set "CANDIDATES="
for %%D in ("%~dp0..\..", "%~dp0..\..\..") do (
  if exist "%%~fD\.venv\Scripts\python.exe" if not defined PY set "PY=%%~fD\.venv\Scripts\python.exe"
  if exist "%%~fD\python_embeded\python.exe" if not defined PY set "PY=%%~fD\python_embeded\python.exe"
  if exist "%%~fD\ComfyUI\.venv\Scripts\python.exe" if not defined PY set "PY=%%~fD\ComfyUI\.venv\Scripts\python.exe"
  if exist "%%~fD\ComfyUI_windows_portable\python_embeded\python.exe" if not defined PY set "PY=%%~fD\ComfyUI_windows_portable\python_embeded\python.exe"
)
if not defined PY (
  for %%P in ("python.exe", "py.exe") do (
    where %%~P >NUL 2>&1 && if not defined PY for /f "delims=" %%F in ('where %%~P') do if not defined PY set "PY=%%F"
  )
)
goto :eof
