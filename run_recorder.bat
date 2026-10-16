@echo off
rem ---- wa-call-recorder launcher (double-click me) ---------------------------
rem 1) makes sure dependencies are installed
rem 2) shows your devices
rem 3) starts recording; press Ctrl+C in this window to stop and save
title wa-call-recorder
cd /d "%~dp0"

python -c "import pyaudiowpatch, numpy" 2>nul
if errorlevel 1 (
    echo Installing dependencies, please wait...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies. Check your Python install.
        pause
        exit /b 1
    )
)

cls
echo ================= wa-call-recorder =================
echo Your devices:
echo.
python wa_call_recorder.py devices
echo.
echo Tip: to prove the recorder really captures sound, run this first:
echo        python wa_call_recorder.py selftest   (plays a short beep)
echo.
echo Now start your WhatsApp Desktop call.
echo Recording starts as soon as you press a key below.
echo.
echo STOP WITH Ctrl+C  (that saves the file).
echo If Windows then asks "Terminate batch job (Y/N)?" answer N
echo (the recording is already saved either way).
echo Closing this window instead may lose the recording.
echo Files are saved into the "recordings" folder here.
echo.
pause
cls
echo Recording... press Ctrl+C to stop.
python wa_call_recorder.py record

echo.
echo Done. Files in the "recordings" folder (newest first):
for /f "delims=" %%f in ('dir /b /o-d "recordings\call_*.wav" 2^>nul ^| findstr /v /i "\.tmp\.wav"') do (
    echo   recordings\%%f
    goto :listed
)
echo   (no finished recording found - always stop with Ctrl+C)
:listed
pause
