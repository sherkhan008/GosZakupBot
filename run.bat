@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"

echo Installing/checking dependencies...
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q

echo Starting GosZakup monitoring bot...
echo Press Ctrl+C to stop.
python -m app.main %*

endlocal
