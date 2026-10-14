"""
Configuration locale — copier ce fichier en `config.py` (non versionné) et
renseigner les valeurs. Si `config.py` est absent, server.py tourne sans
intégration Home Assistant.
"""

HA_ENABLED = True

# Broker MQTT (add-on Mosquitto de Home Assistant)
MQTT_HOST = "192.168.0.205"
MQTT_PORT = 1883
MQTT_USER = "domyos"
MQTT_PASSWORD = "<mot de passe de l'utilisateur HA 'domyos'>"
MQTT_DISCOVERY_PREFIX = "homeassistant"   # préfixe MQTT Discovery configuré dans HA
MQTT_TOPIC_PREFIX = "domyos/woodrower"    # topics : <prefix>/state, /status, /last_session, /availability

# Webhook HA appelé en fin de séance (POST JSON des agrégats de la session)
HA_WEBHOOK_URL = "http://192.168.0.205/api/webhook/<id du webhook>"

# Cadence de publication des métriques live (secondes)
MQTT_LIVE_INTERVAL_S = 1.0

# Écran du kiosque (nom xrandr : xrandr --listmonitors) pour ./domyos.sh start
KIOSK_SCREEN = "DP-1"
