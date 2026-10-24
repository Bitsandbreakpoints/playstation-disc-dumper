@echo off
rem Builds "dist\Playstation Disc Dumper.exe" - a single file with chdman bundled inside.
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".buildenv\Scripts\python.exe" (
    echo Creating build environment...
    python -m venv .buildenv || goto :fail
    ".buildenv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check pyinstaller || goto :fail
)

".buildenv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Playstation Disc Dumper" ^
    --icon "%ROOT%assets\icon.ico" ^
    --add-data "%ROOT%assets\icon.ico;assets" ^
    --add-binary "%ROOT%tools\chdman.exe;tools" ^
    --distpath "%ROOT%dist" --workpath "%ROOT%build" --specpath "%ROOT%build" ^
    "%ROOT%PlaystationDiscDumper.pyw" || goto :fail

echo.
echo Done: dist\Playstation Disc Dumper.exe
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
