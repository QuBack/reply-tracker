@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo Не удалось установить библиотеки. Проверьте, что установлен Python 3.11+ и он добавлен в PATH.
    pause
    exit /b 1
)
echo.
echo Готово. Запускайте run.bat
pause
