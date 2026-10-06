#!/bin/sh
# Po přihlášení do plochy: načíst a spustit bota a timer.
# Domovský adresář je šifrovaný (ecryptfs) a odemkne se až přihlášením.
# Systemd uživatele (linger) ale startuje dřív, ~/.config/systemd/user ještě
# nevidí, a bot ani timer se po rebootu samy nespustí (1. 10. 2026 tak stály
# 5 dní). Tenhle skript to po přihlášení dorovná; spouští ho
# ~/.config/autostart/tipdivize.desktop.
# Instalace (jednorázově):  cp tipdivize.desktop ~/.config/autostart/
systemctl --user daemon-reload
systemctl --user start tipdivize-bot.service tipdivize-update.timer
if systemctl --user is-active -q tipdivize-bot.service && systemctl --user is-active -q tipdivize-update.timer; then
    notify-send "Tipdivize" "Bot a update běží." 2>/dev/null || true
else
    notify-send -u critical "Tipdivize NEBĚŽÍ" "systemctl --user status tipdivize-bot tipdivize-update.timer" 2>/dev/null || true
fi
