@echo off
title AI Assistant Platform - CLI
set PROJ=%~dp0

echo.
echo  ================================================
echo   Amnesty International Greece - AI Assistant
echo  ================================================
echo.
echo  Starting services...
echo.

REM 1. Web server (uvicorn) - webhooks + Zoom app endpoint
start "AI Assistant - Server" /d "%PROJ%" cmd /k "uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload"

REM 2. Public tunnel (Cloudflare named tunnel) - permanent URL, no interstitial
start "AI Assistant - Tunnel" cmd /k "cloudflared tunnel run amnesty-ai"

REM 3. Discord bot
start "AI Assistant - Discord" /d "%PROJ%" cmd /k "ai-assistant discord run"

echo  [1] Server    - http://localhost:8000
echo  [2] Tunnel    - https://ai.ai-assistant-amnesty.xyz
echo  [3] Discord   - AI Assistant#0538
echo  [4] CLI       - this window
echo.
echo  Common commands:
echo    ai-assistant invite --dates "2026-06-17, 2026-06-29"
echo    ai-assistant invite resume --meeting-ref DSxx-YYYY
echo    ai-assistant minutes build DSxx-YYYY --manifest ...
echo.
