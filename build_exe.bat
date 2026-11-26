@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name HarmonicaPlayer main.py || exit /b 1
xcopy /E /I /Y scores "dist\HarmonicaPlayer\scores" >nul
echo.
echo 打包完成：dist\HarmonicaPlayer\HarmonicaPlayer.exe
pause