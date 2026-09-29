@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller -r requirements.txt || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Automation ^
    --distpath outputs --workpath build --specpath build ^
    --add-data "%~dp0.agents\skills\supplier-discovery\SKILL.md;.agents\skills\supplier-discovery" ^
    --add-data "%~dp0automation\windows_ocr.ps1;automation" ^
    --hidden-import pdfplumber --hidden-import pypdfium2 ^
    main.py || goto :error
echo.
echo Готово: outputs\Automation.exe
pause
exit /b 0

:error
echo.
echo Сборка не удалась, смотрите сообщения выше.
pause
exit /b 1
