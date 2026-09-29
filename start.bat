@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Система автоматизации

echo.
echo [1/4] Python
call :find_python
if defined PY goto :python_ok
echo   Python не найден, устанавливаю. Это займёт пару минут...
call :need_winget || goto :fail
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
if not defined PY goto :fail_python
:python_ok
echo   OK: %PY%

echo.
echo [2/4] Библиотеки
"%PY%" -c "import openpyxl, pdfplumber, pypdfium2" >nul 2>&1
if not errorlevel 1 goto :libs_ok
echo   Устанавливаю библиотеки...
"%PY%" -m pip install --disable-pip-version-check --upgrade -r requirements.txt
if errorlevel 1 goto :fail_libs
:libs_ok
echo   OK

echo.
echo [3/4] Codex для поиска поставщиков
call :find_codex
if defined CODEX goto :codex_ok
where npm >nul 2>&1
if not errorlevel 1 goto :npm_ok
echo   Устанавливаю Node.js. Если Windows спросит разрешение, нажмите «Да»...
call :need_winget || goto :codex_skip
winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
:npm_ok
set "PATH=%ProgramFiles%\nodejs;%APPDATA%\npm;%PATH%"
echo   Устанавливаю Codex...
call npm install -g --include=optional @openai/codex
call :find_codex
if not defined CODEX goto :codex_skip
:codex_ok
echo   OK: %CODEX%
"%CODEX%" login status 2>&1 | findstr /c:"Logged in" >nul
if not errorlevel 1 goto :login_ok
echo.
echo   Нужно войти в Codex. Откроется браузер: войдите в свой аккаунт ChatGPT
echo   и вернитесь в это окно.
"%CODEX%" login
:login_ok
goto :launch

:codex_skip
echo   Не удалось установить Codex. Программа запустится, но поиск поставщиков
echo   работать не будет. Инструкция: README.md, раздел «Если что-то не так».
timeout /t 10

:launch
echo.
echo [4/4] Запуск программы
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%~dp0main.py"
exit /b 0

:find_python
set "PY="
for /f "delims=" %%i in ('python -c "import sys, tkinter; print(sys.executable) if sys.version_info >= (3, 11) else None" 2^>nul') do set "PY=%%i"
if defined PY exit /b 0
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%d\python.exe" set "PY=%%d\python.exe"
exit /b 0

:find_codex
set "CODEX="
for /f "delims=" %%i in ('call "%PY%" -c "from automation.supplier_search import find_codex_executable as f; print(f() or '')"') do set "CODEX=%%i"
exit /b 0

:need_winget
where winget >nul 2>&1 && exit /b 0
echo   В Windows нет winget. Установите «Установщик приложений» из Microsoft Store
echo   и запустите start.bat снова.
exit /b 1

:fail_python
echo.
echo Python установить не удалось. Скачайте его с https://www.python.org/downloads/
echo При установке отметьте «Add python.exe to PATH», затем запустите start.bat снова.
goto :fail

:fail_libs
echo.
echo Не удалось установить библиотеки. Проверьте интернет и запустите start.bat снова.
goto :fail

:fail
echo.
pause
exit /b 1
