#!/bin/bash
# test.sh — cu-tools v2 冒烟测试 (无 UI 副作用: 不点击/不抢前台)
# 用法: bash test.sh   (可用 CU_TOOLS_BIN 覆盖工具目录, 默认 ~/.astra/bin)
set -uo pipefail
BIN="${CU_TOOLS_BIN:-$HOME/.astra/bin}"
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "PASS: $1"; }
bad() { fail=$((fail+1)); echo "FAIL: $1"; }

# 1. cuwin -l 列出窗口 (含微信)
if "$BIN/cuwin" -l 2>/dev/null | grep -qE "微信|WeChat"; then
  ok "cuwin -l 列出窗口(含微信)"
else
  bad "cuwin -l 无微信窗口"
fi

# 2. cuwin 查询不抢前台
before=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true')
"$BIN/cuwin" 微信 --first >/dev/null 2>&1 || "$BIN/cuwin" WeChat --first >/dev/null 2>&1
after=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true')
if [ "$before" = "$after" ]; then ok "cuwin 查询不抢前台 (frontmost=$before)"; else bad "cuwin 抢前台: $before -> $after"; fi

# 3. cuocr 输出全局逻辑坐标 (顶部区域, 坐标应全部落在 600x300 内)
out=$("$BIN/cuocr" 0 0 600 300 2>/dev/null | head -5)
if [ -n "$out" ] && echo "$out" | awk -F'\t' '{split($3,a,","); if (a[1]+0 > 600 || a[2]+0 > 300 || a[1]+0 < 0) exit 1}'; then
  ok "cuocr 全局逻辑坐标 (样例: $(echo "$out" | head -1 | cut -f1-3 | tr '\t' ' '))"
else
  bad "cuocr 坐标异常: $out"
fi

# 4. cuclip 文件 -> furl 验证
tmp="/tmp/cutest_$$.txt"
echo "cu-tools v2 test" > "$tmp"
if "$BIN/cuclip" "$tmp" >/dev/null 2>&1 && osascript -e 'set f to (the clipboard as «class furl»)' -e 'log f' 2>&1 | grep -q "cutest_$$"; then
  ok "cuclip 文件 -> 剪贴板 furl 验证"
else
  bad "cuclip 失败"
fi
rm -f "$tmp"

# 5. cuwait 出现 (菜单栏稳定元素"Shell", 前台=Terminal 时) / 假词超时 exit 1
if "$BIN/cuwait" "Shell" -x 0 -y 0 -w 400 -h 120 --timeout 10 >/dev/null 2>&1; then
  ok "cuwait 文本出现"
else
  bad "cuwait 出现超时"
fi
if "$BIN/cuwait" "qzxwv_nonexist_xyz" -x 0 0 900 600 --timeout 3 >/dev/null 2>&1; then
  bad "cuwait 假词未超时"
else
  ok "cuwait 超时 exit 1"
fi

# 6. cuclick usage 正常 (避开 pipefail: 先存输出再 grep)
cu_out=$("$BIN/cuclick" 2>&1 || true)
echo "$cu_out" | grep -q "usage" && ok "cuclick usage 正常" || bad "cuclick usage 异常"

echo "== $pass passed, $fail failed =="
[ $fail -eq 0 ]
