@echo off
rem Rebuild MousePet as a FOLDER build (no temp extraction at startup).
rem Needs: Python 3.9+ with pip on PATH.
cd /d "%~dp0"

echo.
echo  [1/2] Installing / updating PyInstaller...
python -m pip install --user --upgrade pyinstaller
if errorlevel 1 goto :fail

echo.
echo  [2/2] Building MousePet (folder build)...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name MousePet --add-data "assets;assets" mouse_pet.py
if errorlevel 1 goto :fail

echo.
echo  Done: dist\MousePet\MousePet.exe
echo.
echo  Send the WHOLE dist\MousePet folder (zip it).
echo  The _internal folder must stay next to the exe.
echo  First run creates data\ inside that folder.
echo.
pause
exit /b 0

:fail
echo.
echo  Build failed. Check that Python and pip are installed and on PATH.
echo.
pause
exit /b 1
