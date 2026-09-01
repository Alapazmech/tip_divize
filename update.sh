#!/bin/sh
# Denní update: výsledky -> odhalení tiketů dohraných kol -> kurzy -> stránka.
set -e
cd "$(dirname "$0")"
python3 scraper.py season
python3 tickets.py
python3 odds.py
python3 generate_site.py
