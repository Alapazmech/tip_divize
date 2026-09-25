#!/bin/sh
# Jen stránka: přegenerovat -> commit -> push -> Render. Bez scrapu a kurzů.
# Bot ho pouští po každém přijatém tiketu a dokupu; stejný zámek jako update.sh.
set -e
cd "$(dirname "$0")"
exec 9>.update.lock
flock -w 600 9 || { echo "publish.sh: jiný update ještě běží" >&2; exit 1; }
python3 generate_site.py >/dev/null
git add -A
git diff --cached --quiet || git commit -q -m "Stránka $(date '+%Y-%m-%d %H:%M')"
git push -q origin main 2>/dev/null || echo "(push se nepovedl — nasadí se při příštím updatu)"
[ -f data/render_hook.txt ] && curl -s "$(cat data/render_hook.txt)" >/dev/null 2>&1 || true
