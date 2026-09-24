@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if not %ERRORLEVEL%==0 (
    echo Python 3 was not found.
    pause
    exit /b 1
  )
  set "PY=python"
)

echo ============================================================
echo ETE v13.72 - FINAL VERIFICATION REWORK GATE
echo ============================================================
echo.
echo Run this ONCE on the CENTRAL SERVER PC.
echo It will create a backup before changing server.py.
echo.
set /p SERVERFILE=Enter the full path to your CURRENT production server.py: 
if "%SERVERFILE%"=="" (
  echo No server.py path entered.
  pause
  exit /b 2
)

%PY% "%~dp0patch_server_v13_72.py" "%SERVERFILE%"
if not %ERRORLEVEL%==0 (
  echo.
  echo SERVER PATCH FAILED. Your original server.py was not intentionally replaced.
  pause
  exit /b 1
)

echo.
echo Patch completed.
echo Restart the Central Server application before using MAC Rewrite.
pause
