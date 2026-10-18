#!/bin/bash

# Quick start script - run the server

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found!"
    echo "Run ./setup.sh first to install dependencies"
    exit 1
fi

echo "🎬 Starting Clip Maker Pro..."
echo ""

source .venv/bin/activate
# Lets Pillow's text layout find Homebrew's FriBiDi (Hindi, Arabic, Bengali captions).
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:/usr/local/lib:/usr/lib"
uvicorn app:app --reload --port 8000
