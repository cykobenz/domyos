#!/usr/bin/env bash
# Domyos Woodrower — lanceur unique
#
#   ./domyos.sh start      serveur (service systemd utilisateur, ou arrière-plan) + kiosque plein écran
#                          sur KIOSK_SCREEN + fenêtre de contrôle (control.html) sur CONTROL_SCREEN
#   ./domyos.sh kiosk      seulement les deux fenêtres (le serveur tourne déjà)
#   ./domyos.sh control    seulement la fenêtre de contrôle
#   ./domyos.sh stop       ferme les fenêtres (le serveur continue : il se reconnecte seul au rameur)
#   ./domyos.sh stop-all   ferme les fenêtres ET arrête le serveur
#   ./domyos.sh status     état du serveur, du rameur, du kiosque
#   ./domyos.sh logs       suit les logs du serveur
#   ./domyos.sh install    installe + active le service systemd utilisateur domyos-server (démarre à l'ouverture de session)
#   ./domyos.sh uninstall  retire le service
#
# Les écrans viennent de KIOSK_SCREEN / CONTROL_SCREEN dans config.py (noms xrandr, ex.
# "DP-1"), ou des variables d'environnement DOMYOS_SCREEN / DOMYOS_CONTROL_SCREEN. Brave est
# lancé avec des profils dédiés (~/.config/domyos-kiosk, ~/.config/domyos-control) pour que
# les flags (autoplay → bip) s'appliquent même si un autre Brave est ouvert. Le placement
# et le plein écran sont forcés avec xdotool (Chromium ignore --window-position en kiosque).

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${DOMYOS_PORT:-8765}"
URL="http://localhost:${PORT}/"
UNIT="domyos-server"
UNIT_FILE="$HOME/.config/systemd/user/${UNIT}.service"
LOG="$DIR/server.log"
PROFILE="$HOME/.config/domyos-kiosk"
PROFILE_CTRL="$HOME/.config/domyos-control"
BRAVE="$(command -v brave-browser || command -v brave || command -v chromium || command -v google-chrome || true)"

config_value() {   # config_value KIOSK_SCREEN DP-1
  python3 -c "import sys; sys.path.insert(0, '$DIR'); import config; print(getattr(config, '$1', '$2'))" 2>/dev/null || echo "$2"
}
screen_name()  { echo "${DOMYOS_SCREEN:-$(config_value KIOSK_SCREEN DP-1)}"; }
control_name() { echo "${DOMYOS_CONTROL_SCREEN:-$(config_value CONTROL_SCREEN HDMI-1)}"; }

# profil Brave neuf : pas de bandeau « analyses de produits » ni de page d'accueil
seed_profile() {
  local dir="$1"
  [ -d "$dir" ] && return 0
  mkdir -p "$dir/Default"
  echo '{"brave":{"p3a":{"notice_acknowledged":true,"enabled":false},"stats":{"reporting_enabled":false}}}' >"$dir/Local State"
  echo '{"brave":{"p3a":{"notice_acknowledged":true},"welcome_page":{"shown":true}},"browser":{"has_seen_welcome_page":true}}' >"$dir/Default/Preferences"
}

# open_window <profil> <url> <classe WM> <écran> <fullscreen 0/1> [largeur hauteur]
open_window() {
  local profile="$1" url="$2" cls="$3" screen="$4" full="$5" ww="${6:-}" wh="${7:-}"
  local geo w h x y
  seed_profile "$profile"
  geo="$(screen_geometry "$screen")"
  if [ -n "$geo" ]; then
    read -r w h x y <<<"$geo"
  else
    echo "⚠ écran « ${screen:-non défini} » introuvable dans xrandr (xrandr --listmonitors) → écran courant" >&2
    w=1920; h=1080; x=0; y=0
  fi
  if [ "$full" = 1 ]; then ww=$w; wh=$h; else x=$((x + (w - ww) / 2)); y=$((y + (h - wh) / 2)); fi
  nohup "$BRAVE" --user-data-dir="$profile" --class="$cls" --no-first-run --app="$url" \
    --autoplay-policy=no-user-gesture-required --disable-session-crashed-bubble \
    --window-position="${x},${y}" --window-size="${ww},${wh}" >/dev/null 2>&1 &
  disown
  if command -v xdotool >/dev/null; then
    local wid=""
    for _ in $(seq 1 40); do
      wid="$(xdotool search --onlyvisible --class "$cls" 2>/dev/null | head -1 || true)"   # pipefail : search renvoie 1 tant que rien
      [ -n "$wid" ] && break
      sleep 0.25
    done
    if [ -n "$wid" ]; then
      xdotool windowmove "$wid" "$x" "$y" >/dev/null 2>&1 || true
      xdotool windowsize "$wid" "$ww" "$wh" >/dev/null 2>&1 || true
      if [ "$full" = 1 ]; then
        sleep 0.8
        xdotool windowmove "$wid" "$x" "$y" >/dev/null 2>&1 || true
        # F11 = plein écran Chromium ; envoyé comme vraie frappe (XTEST) à la fenêtre activée,
        # car Chromium ignore les événements synthétiques de `key --window`
        xdotool windowactivate --sync "$wid" >/dev/null 2>&1 || true
        xdotool key --clearmodifiers F11 >/dev/null 2>&1 || true
      fi
    else
      echo "⚠ fenêtre $cls non trouvée par xdotool (placement non forcé)" >&2
    fi
  fi
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
kiosk_pid()   { pgrep -f -- "--user-data-dir=${PROFILE} " | head -1 || true; }
control_pid() { pgrep -f -- "--user-data-dir=${PROFILE_CTRL} " | head -1 || true; }

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
  if [ -n "$(kiosk_pid)" ]; then echo "kiosque : déjà ouvert"; else
    echo "kiosque : écran $(screen_name), plein écran"
    open_window "$PROFILE" "$URL" "domyos-kiosk" "$(screen_name)" 1
  fi
}

cmd_control() {
  [ -z "$BRAVE" ] && { echo "⚠ Brave / Chromium introuvable" >&2; return 1; }
  if [ -n "$(control_pid)" ]; then echo "contrôle : déjà ouvert"; else
    echo "contrôle : écran $(control_name), fenêtre 1280×900"
    open_window "$PROFILE_CTRL" "${URL}control.html" "domyos-control" "$(control_name)" 0 1280 900
  fi
}

cmd_windows_stop() {
  local n=0
  [ -n "$(kiosk_pid)" ]   && { pkill -f -- "--user-data-dir=${PROFILE} " || true; n=$((n+1)); }
  [ -n "$(control_pid)" ] && { pkill -f -- "--user-data-dir=${PROFILE_CTRL} " || true; n=$((n+1)); }
  [ "$n" -gt 0 ] && echo "fenêtres : fermées" || echo "fenêtres : aucune ouverte"
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
  [ -n "$(kiosk_pid)" ] && echo "kiosque  : ouvert ($(screen_name))" || echo "kiosque  : fermé"
  [ -n "$(control_pid)" ] && echo "contrôle : ouvert ($(control_name))" || echo "contrôle : fermé"
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
  start)     cmd_server_start; cmd_kiosk; cmd_control ;;
  kiosk)     cmd_kiosk; cmd_control ;;
  control)   cmd_control ;;
  stop)      cmd_windows_stop ;;
  stop-all)  cmd_windows_stop; cmd_server_stop ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
