#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing/checking dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "Starting GosZakup monitoring bot..."
echo "Press Ctrl+C to stop."
python -m app.main "$@"
