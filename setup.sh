#!/bin/bash

# Clip Maker Pro - One-command setup script

set -e

echo "🎬 Clip Maker Pro Setup"
echo "======================="
echo ""

# Check if Python 3.12 is installed
if ! command -v /opt/homebrew/bin/python3.12 &> /dev/null; then
    echo "❌ Python 3.12 not found"
    echo "Installing Python 3.12 via Homebrew..."
    brew install python@3.12
    echo "✅ Python 3.12 installed"
else
    echo "✅ Python 3.12 found"
fi

# Check if ffmpeg is installed
if ! command -v ffmpeg &> /dev/null; then
    echo "❌ ffmpeg not found"
    echo "Installing ffmpeg via Homebrew..."
    brew install ffmpeg
    echo "✅ ffmpeg installed"
else
    echo "✅ ffmpeg found"
fi

# FriBiDi lets captions render Hindi, Arabic, Bengali and similar scripts
if ! brew list fribidi &> /dev/null; then
    echo "Installing FriBiDi via Homebrew (text shaping for non-Latin captions)..."
    brew install fribidi
    echo "✅ FriBiDi installed"
else
    echo "✅ FriBiDi found"
fi

echo ""
echo "Setting up virtual environment..."

# Remove old venv if exists
if [ -d ".venv" ]; then
    echo "Removing old virtual environment..."
    chmod -R u+w .venv 2>/dev/null || true
    rm -rf .venv
fi

# Create new venv with Python 3.12
/opt/homebrew/bin/python3.12 -m venv .venv
echo "✅ Virtual environment created"

# Activate and upgrade pip
source .venv/bin/activate
echo "Upgrading pip..."
pip install --upgrade pip --quiet

# Install dependencies
echo ""
echo "Installing dependencies..."
echo "(This may take a few minutes)"
pip install -r requirements.txt

echo ""
echo "✅ All dependencies installed!"
echo ""

# Face-detection model for 9:16 reframing (checksum-verified)
bash scripts/fetch_models.sh
echo ""

# Initialize database
echo "Initializing database..."
python -c "
import db

db.init()
print('✅ Database initialized')
"

echo ""
echo "🎉 Setup complete!"
echo ""
echo "To start the server:"
echo "  source .venv/bin/activate"
echo "  uvicorn app:app --reload --port 8000"
echo ""
echo "Then open: http://localhost:8000"
echo ""
echo "📚 Documentation:"
echo "  - PRO_FEATURES.md - Professional features guide"
echo "  - INSTALL.md - Installation & usage"
echo "  - UI_REDESIGN.md - UI design notes"
echo ""
