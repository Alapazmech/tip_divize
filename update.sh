#!/bin/sh
# Denní update: výsledky -> odhalení tiketů -> kurzy -> stránka -> push (Render).
set -e
cd "$(dirname "$0")"
python3 scraper.py season
python3 tickets.py
python3 odds.py
python3 generate_site.py
git add -A
git diff --cached --quiet || git commit -q -m "Update dat $(date '+%Y-%m-%d %H:%M')"
git push -q origin main 2>/dev/null || echo "(push se nepovedl — nasadí se při příštím updatu)"
# Render nemá na repu webhook — nasazení budí deploy hook (URL je tajná, gitignored)
[ -f data/render_hook.txt ] && curl -s "$(cat data/render_hook.txt)" >/dev/null 2>&1 || true
