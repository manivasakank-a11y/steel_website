@echo off
REM Put this file in the steel_website folder (next to app.py) and double-click it.
cd /d "%~dp0"

echo Starting the website...
start "Steel website - keep open" cmd /k python serve.py

timeout /t 5 /nobreak >nul

where cloudflared >nul 2>nul
if %errorlevel%==0 (
    echo Starting the public link...
    start "Public link - copy the https://....trycloudflare.com address" cmd /k cloudflared tunnel --url http://127.0.0.1:8080
) else (
    echo.
    echo cloudflared is not installed, so there is no public link.
    echo The website works on this PC at http://heatnumbertracking:8080
    echo To get a public link install it once with:  winget install Cloudflare.cloudflared
    echo.
    pause
)