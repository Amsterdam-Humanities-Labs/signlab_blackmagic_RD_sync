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

build_one() {
    local src=$1 out=$2
    clang++ -std=c++17 -O2 \
        -I"$SDK/Include" \
        "$SDK/Include/BlackmagicRawAPIDispatch.cpp" \
        "$src" \
        -framework CoreFoundation \
        -o "$out"
    echo "built: $out"
}

build_one src/braw2hevc.cpp build/braw2hevc
build_one src/braw_probe.cpp build/braw_probe
