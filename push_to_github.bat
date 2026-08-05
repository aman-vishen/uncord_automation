@echo off
setlocal
cd /d "%~dp0"
where git >nul 2>nul || (
  echo Git is not installed or not in PATH.
  pause
  exit /b 1
)
git remote remove origin >nul 2>nul
git remote add origin git@github.com:aman-vishen/uncord_automation.git
git branch -M main
git push -u origin main
if errorlevel 1 (
  echo.
  echo Push failed. Confirm your GitHub SSH key is installed and the repository exists.
  pause
  exit /b 1
)
echo Push completed successfully.
pause
