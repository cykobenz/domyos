#!/usr/bin/env bash
# Domyos Woodrower — lanceur unique
#
#   ./domyos.sh start      serveur (service systemd utilisateur, ou arrière-plan) + kiosque sur l'écran KIOSK_SCREEN
#   ./domyos.sh kiosk      seulement le kiosque (le serveur tourne déjà)
#   ./domyos.sh stop       ferme le kiosque (le serveur continue : il se reconnecte seul au rameur)
#   ./domyos.sh stop-all   ferme le kiosque ET arrête le serveur
#   ./domyos.sh status     état du serveur, du rameur, du kiosque
#   ./domyos.sh logs       suit les logs du serveur
#   ./domyos.sh install    installe + active le service systemd utilisateur domyos-server (démarre à l'ouverture de session)
#   ./domyos.sh uninstall  retire le service
#
# L'écran du kiosque vient de KIOSK_SCREEN dans config.py (nom xrandr, ex. "DP-1"), ou de
# la variable d'environnement DOMYOS_SCREEN. Brave est lancé avec son propre profil
# (~/.config/domyos-kiosk) pour que les flags (autoplay → bip) s'appliquent même si un
# autre Brave est ouvert.

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${DOMYOS_PORT:-8765}"
URL="http://localhost:${PORT}/"
UNIT="domyos-server"
UNIT_FILE="$HOME/.config/systemd/user/${UNIT}.service"
LOG="$DIR/server.log"
PROFILE="$HOME/.config/domyos-kiosk"
BRAVE="$(command -v brave-browser || command -v brave || command -v chromium || command -v google-chrome || true)"

screen_name() {
  if [ -n "${DOMYOS_SCREEN:-}" ]; then echo "$DOMYOS_SCREEN"; return; fi
  python3 -c "import sys; sys.path.insert(0, '$DIR'); import config; print(getattr(config, 'KIOSK_SCREEN', ''))" 2>/dev/null || true
}

# géométrie xrandr de l'écran → "W H X Y" (vide si introuvable)
screen_geometry() {
  local name="$1"
  [ -z "$name" ] && return 0
  xrandr --listmonitors 2>/dev/null | awk -v n="$name" '
    $NF == n { split($3, a, /[\/x+]/); print a[1], a[3], a[5], a[6] }'
}

unit_installed() { [ -f "$UNIT_FILE" ]; }
server_up() { curl -sf --max-time 2 "${URL}api/state" >/dev/null 2>&1; }
kiosk_pid() { pgrep -f -- "--user-data-dir=${PROFILE}" | head -1 || true; }

cmd_server_start() {
  if server_up; then echo "serveur : déjà en ligne sur $URL"; return; fi
  if unit_installed; then
    systemctl --user start "$UNIT"
    echo "serveur : service $UNIT démarré (logs : ./domyos.sh logs)"
  else
    (cd "$DIR" && nohup python3 -u server.py --port "$PORT" >>"$LOG" 2>&1 &)
    echo "serveur : lancé en arrière-plan (logs : $LOG)"
  fi
  for _ in $(seq 1 20); do server_up && return; sleep 0.5; done
  echo "⚠ le serveur ne répond pas sur $URL" >&2; return 1
}

cmd_server_stop() {
  if unit_installed && systemctl --user is-active --quiet "$UNIT"; then
    systemctl --user stop "$UNIT"; echo "serveur : service arrêté"
  elif pgrep -f "python3 -u server.py" >/dev/null || pgrep -f "python3 server.py" >/dev/null; then
    pkill -INT -f "python3( -u)? server.py" || true; echo "serveur : arrêté"
  else
    echo "serveur : pas lancé"
  fi
}

cmd_kiosk() {
  [ -z "$BRAVE" ] && { echo "⚠ Brave / Chromium introuvable" >&2; return 1; }
  if [ -n "$(kiosk_pid)" ]; then echo "kiosque : déjà ouvert"; return; fi
  local name geo pos=() ; name="$(screen_name)"; geo="$(screen_geometry "$name")"
  if [ -n "$geo" ]; then
    read -r w h x y <<<"$geo"
    pos=(--window-position="${x},${y}" --window-size="${w},${h}")
    echo "kiosque : écran $name (${w}x${h} en +${x}+${y})"
  else
    echo "kiosque : écran « ${name:-non défini} » introuvable dans xrandr → écran courant"
  fi
  nohup "$BRAVE" --user-data-dir="$PROFILE" --no-first-run --kiosk --app="$URL" \
    --autoplay-policy=no-user-gesture-required --disable-session-crashed-bubble \
    "${pos[@]}" >/dev/null 2>&1 &
  disown
}

cmd_kiosk_stop() {
  local pid; pid="$(kiosk_pid)"
  if [ -n "$pid" ]; then pkill -f -- "--user-data-dir=${PROFILE}" || true; echo "kiosque : fermé"; else echo "kiosque : pas ouvert"; fi
}

cmd_status() {
  if server_up; then
    curl -sf --max-time 2 "${URL}api/state" | python3 -c "
import json, sys
d = json.load(sys.stdin)
rower = 'connecté' if d['connected'] else 'hors ligne'
ha = 'ok' if d.get('ha_connected') else 'non'
print(f\"serveur  : en ligne — rameur {rower}, état {d['machine_state']}, {d['clients']} client(s) WS, HA {ha}\")
p = d.get('program')
print(f\"programme: {p['name']} — {p['label']} ({p['step']}/{p['steps']}), reste {p['remaining_s']} s\" if p else 'programme: aucun')"
  else
    echo "serveur  : hors ligne"
  fi
  unit_installed && echo "service  : $UNIT $(systemctl --user is-enabled "$UNIT" 2>/dev/null || true) / $(systemctl --user is-active "$UNIT" 2>/dev/null || true)" || echo "service  : non installé (./domyos.sh install)"
  [ -n "$(kiosk_pid)" ] && echo "kiosque  : ouvert" || echo "kiosque  : fermé"
}

cmd_logs() {
  if unit_installed; then journalctl --user -u "$UNIT" -f -n 50; else tail -n 50 -f "$LOG"; fi
}

cmd_install() {
  mkdir -p "$(dirname "$UNIT_FILE")"
  cat >"$UNIT_FILE" <<EOF
[Unit]
Description=Domyos Woodrower — serveur BLE / WebSocket / Home Assistant
After=bluetooth.target graphical-session.target

[Service]
WorkingDirectory=$DIR
ExecStart=$(command -v python3) -u $DIR/server.py --port $PORT
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT"
  echo "service $UNIT installé et démarré ($UNIT_FILE)"
  echo "logs : ./domyos.sh logs   —   il démarre maintenant à chaque ouverture de session"
}

cmd_uninstall() {
  if unit_installed; then
    systemctl --user disable --now "$UNIT" || true
    rm -f "$UNIT_FILE"; systemctl --user daemon-reload
    echo "service $UNIT retiré"
  else
    echo "service non installé"
  fi
}

case "${1:-start}" in
  start)     cmd_server_start; cmd_kiosk ;;
  kiosk)     cmd_kiosk ;;
  stop)      cmd_kiosk_stop ;;
  stop-all)  cmd_kiosk_stop; cmd_server_stop ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
