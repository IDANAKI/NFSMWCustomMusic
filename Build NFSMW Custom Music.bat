@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title NFSMW Custom Music - build EXE

echo ==========================================================
echo  NFSMW Custom Music - single EXE builder
echo ==========================================================
echo.

if not exist "%~dp0NFSMW Custom Music.py" (
    echo ERROR: NFSMW Custom Music.py is missing next to this BAT file.
    goto :fail
)
if not exist "%~dp0NFSMW Custom Music Data.py" (
    echo ERROR: NFSMW Custom Music Data.py is missing next to this BAT file.
    goto :fail
)
if not exist "%~dp0Icon.png" (
    echo ERROR: Icon.png is missing next to this BAT file.
    echo Put the PNG icon in this folder and start the BAT again.
    goto :fail
)

set "PY_CMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"
if defined PY_CMD goto :python_found
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"
if defined PY_CMD goto :python_found
echo ERROR: Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/ and try again.
goto :fail

:python_found
echo Using Python:
%PY_CMD% --version

echo Checking build dependencies...
%PY_CMD% -c "import PyInstaller, PIL" >nul 2>&1
if not errorlevel 1 goto :dependencies_ready
echo Installing PyInstaller and Pillow. Internet may be required.
%PY_CMD% -m pip --version >nul 2>&1
if errorlevel 1 %PY_CMD% -m ensurepip --upgrade
%PY_CMD% -m pip install pyinstaller pillow
if errorlevel 1 (
    echo ERROR: Could not install build dependencies.
    goto :fail
)
%PY_CMD% -c "import PyInstaller, PIL" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Build dependencies are not available in this Python.
    goto :fail
)

:dependencies_ready
set "WORK=%TEMP%\NFSMW_Build_%RANDOM%_%RANDOM%"
mkdir "%WORK%" >nul 2>&1
if not exist "%WORK%\" (
    echo ERROR: Could not create a temporary build directory.
    goto :fail
)

echo Preparing the EXE icon from Icon.png...
%PY_CMD% -c "from PIL import Image,ImageOps; import sys; im=Image.open(sys.argv[1]); im.load(); im=ImageOps.contain(im.convert('RGBA'),(256,256),getattr(Image,'Resampling',Image).LANCZOS); bg=Image.new('RGBA',(256,256)); bg.alpha_composite(im,((256-im.width)//2,(256-im.height)//2)); bg.save(sys.argv[2],format='ICO',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" "%~dp0Icon.png" "%WORK%\app.ico"
if errorlevel 1 (
    echo ERROR: Icon.png could not be converted to an EXE icon.
    goto :fail
)

echo.
echo Building one EXE with both Python files inside...
echo This can take several minutes.
%PY_CMD% -m PyInstaller --noconfirm --clean --onefile --windowed --name "NFSMW Custom Music" --icon "%WORK%\app.ico" --paths "." --add-data "%~dp0NFSMW Custom Music Data.py:." --distpath "%~dp0build" --workpath "%WORK%\work" --specpath "%WORK%" "%~dp0NFSMW Custom Music.py"
if errorlevel 1 (
    echo ERROR: PyInstaller reported a build failure.
    goto :fail
)
if not exist "%~dp0build\NFSMW Custom Music.exe" (
    echo ERROR: The expected EXE was not created.
    goto :fail
)

rmdir /s /q "%WORK%" >nul 2>&1
echo.
echo SUCCESS: %~dp0build\NFSMW Custom Music.exe
echo The EXE contains both scripts and the required resources.
echo You can copy the EXE on its own.
echo.
pause
exit /b 0

:fail
echo.
echo Build failed. The files in the game folder were not changed.
if defined WORK echo Temporary files, if any: %WORK%
echo.
pause
exit /b 1
