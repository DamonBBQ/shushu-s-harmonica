@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
%PYTHON% -m pip install -r requirements.txt pyinstaller || exit /b 1
%PYTHON% -m unittest discover -s tests -v || exit /b 1
for /f "delims=" %%V in ('%PYTHON% -c "from core.version import __version__; print(__version__)"') do set "VERSION=%%V"
%PYTHON% -m PyInstaller --noconfirm --clean --windowed --name HarmonicaPlayer main.py || exit /b 1
xcopy /E /I /Y scores "dist\HarmonicaPlayer\scores" >nul
> "dist\HarmonicaPlayer\app-version.txt" echo %VERSION%
set "ZIP=HarmonicaPlayer-%VERSION%-windows-x64.zip"
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Compress-Archive -LiteralPath 'dist\HarmonicaPlayer' -DestinationPath 'dist\%ZIP%' -CompressionLevel Optimal -Force; $hash=(Get-FileHash -LiteralPath 'dist\%ZIP%' -Algorithm SHA256).Hash.ToLowerInvariant(); Set-Content -LiteralPath 'dist\%ZIP%.sha256' -Value $hash -Encoding ascii" || exit /b 1
echo.
echo 打包完成：dist\HarmonicaPlayer\HarmonicaPlayer.exe
echo 发布文件：dist\%ZIP%
echo 校验文件：dist\%ZIP%.sha256
pause
