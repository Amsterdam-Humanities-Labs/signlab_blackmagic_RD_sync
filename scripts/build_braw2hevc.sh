#!/bin/bash
# Build the BRAW -> H.265 transcoder. Run from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."

SDK="/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac"
if [[ ! -d "$SDK" ]]; then
    echo "BRAW SDK not found at: $SDK" >&2
    exit 1
fi

mkdir -p build
clang++ -std=c++17 -O2 \
    -I"$SDK/Include" \
    "$SDK/Include/BlackmagicRawAPIDispatch.cpp" \
    src/braw2hevc.cpp \
    -framework CoreFoundation \
    -o build/braw2hevc

echo "built: build/braw2hevc"
