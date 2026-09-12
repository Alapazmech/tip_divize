#!/bin/sh
# Update: výsledky -> odhalení tiketů -> kurzy -> stránka -> push (Render).
# Spouští ho timer (tipdivize-update.timer), bot na „updatuj kurzy" i člověk
# ručně — zámek zajistí, že běží vždy jen jeden.
set -e
cd "$(dirname "$0")"
exec 9>.update.lock
flock -w 600 9 || { echo "update.sh: jiný update ještě běží" >&2; exit 1; }
python3 scraper.py season
# Když se ze scrapu změnilo jen razítko scraped_at, nezakládat kvůli tomu commit.
if [ -z "$(git diff -U0 -- data/season.json | grep '^[-+][^-+]' | grep -v '"scraped_at"')" ]; then
    git checkout -q -- data/season.json
fi
python3 tickets.py
python3 odds.py ${1:+--force}
python3 generate_site.py
git add -A
git diff --cached --quiet || git commit -q -m "Update dat $(date '+%Y-%m-%d %H:%M')"
git push -q origin main 2>/dev/null || echo "(push se nepovedl — nasadí se při příštím updatu)"
# Render nemá na repu webhook — nasazení budí deploy hook (URL je tajná, gitignored)
[ -f data/render_hook.txt ] && curl -s "$(cat data/render_hook.txt)" >/dev/null 2>&1 || true
