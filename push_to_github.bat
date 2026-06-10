@echo off
echo ============================================
echo   Updating yt-dlp to latest version...
echo ============================================
venv\Scripts\pip.exe install -U yt-dlp
echo.
echo ============================================
echo   Pushing bot fixes to GitHub...
echo ============================================
git add -A
git commit -m "fix: comprehensive bot fixes - YouTube HTML download bug, yt-dlp MP4 format, Pornhub search, search URL extraction, queue closure bugs"
git push origin main
echo.
echo Done! Check output above for any errors.
pause
