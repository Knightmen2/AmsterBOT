@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Пинг https://amsterbot.onrender.com каждые 5 минут
echo Окно не закрывайте. Для остановки: Ctrl+C
python keepalive.py https://amsterbot.onrender.com
pause
