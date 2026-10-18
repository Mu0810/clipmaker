#!/bin/bash
# Download the face-detection model used for 9:16 reframing (design §4.6).
#
# Source: OpenCV's official YuNet model on Hugging Face (opencv/face_detection_yunet).
# The checksum is pinned so a changed or tampered file is refused.
# Pinned on 2026-10-09 from the first verified download: 232,589 bytes, matching
# the size and SHA-256 Hugging Face publishes for this file (x-linked-etag).
set -euo pipefail

cd "$(dirname "$0")/.."

MODEL_URL="https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
MODEL_FILE="models/face_detection_yunet_2023mar.onnx"
MODEL_SHA256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

mkdir -p models
if [ -f "$MODEL_FILE" ] && [ "$(sha256_of "$MODEL_FILE")" = "$MODEL_SHA256" ]; then
    echo "✅ Face model already present"
    exit 0
fi

tmp="$(mktemp "models/.download.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
echo "Downloading face model..."
curl --fail --location --silent --show-error --max-time 300 -o "$tmp" "$MODEL_URL"

actual="$(sha256_of "$tmp")"
if [ "$actual" != "$MODEL_SHA256" ]; then
    echo "❌ Face model checksum mismatch (got $actual, expected $MODEL_SHA256)" >&2
    exit 1
fi
mv "$tmp" "$MODEL_FILE"
trap - EXIT
echo "✅ Face model saved to $MODEL_FILE"
