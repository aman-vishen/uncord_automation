@echo off
setlocal
cd /d "%~dp0mac_rewrite"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if not %ERRORLEVEL%==0 (
    echo Python 3 was not found.
    echo Install Python 3 for Windows and run this file again.
    pause
    exit /b 1
  )
  set "PY=python"
)

echo Checking required Python packages...
%PY% -c "import telnetlib3, paramiko" >nul 2>&1
if not %ERRORLEVEL%==0 (
  echo Installing required packages...
  %PY% -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
  if not %ERRORLEVEL%==0 (
    echo.
    echo Package installation failed.
    pause
    exit /b 1
  )
)

echo.
echo Starting ETE MAC Rewrite v13.72...
%PY% app.py
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo MAC Rewrite exited with error code %RC%.
  pause
)
exit /b %RC%
