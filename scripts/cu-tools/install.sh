#!/bin/bash
# install.sh — 安装 cu-tools v2 到 ~/.astra/bin/ (macOS GUI 自动化通用工具)
# 用法: bash scripts/cu-tools/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${HOME}/.astra/bin"
mkdir -p "$DST"

echo "==> 编译 Swift 工具 (cuclick/cuwin/cuclip)"
swiftc -O "$SRC/cuclick.swift" -o "$DST/cuclick"
swiftc -O "$SRC/cuwin.swift"   -o "$DST/cuwin"
swiftc -O "$SRC/cuclip.swift"  -o "$DST/cuclip"

echo "==> 安装脚本工具"
cp "$SRC/cushot" "$SRC/cuocr" "$SRC/cuwait" "$DST/"
chmod +x "$DST/cushot" "$DST/cuocr" "$DST/cuwait"

echo "==> 完成:"
ls -la "$DST" | grep -E "cuclick|cushot|cuocr|cuwin|cuclip|cuwait"
echo "用法: cuclick x y [left|right|double] [-f app] | cushot x y w h [out] | cuocr x y w h [--raw]"
echo "      cuwin -l | cuwin <关键字> | cuclip <path> | cuwait <text> [--absent] ..."
