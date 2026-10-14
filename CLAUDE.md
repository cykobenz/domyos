# Domyos Woodrower — Projet Linux

## Contexte

Remplacement de Kinomap par une stack Python maison pour piloter et visualiser les données du rameur **Domyos Woodrower** depuis le desktop Linux (Ubuntu 24.04 LTS, kernel 7.0.0-38). Le rameur est positionné à côté du bureau, face à une télé utilisée comme 3ème écran.

## Matériel

- **Rameur** : Domyos Woodrower (Decathlon)
  - Protocole : Bluetooth BLE, profil **FTMS** (Fitness Machine Service, UUID `0x1826`)
  - MAC : `1C:2A:85:46:23:5A` (nom BLE : `Domyos-ROW-0743`)
  - Dongle BT : CSR8510 A10 (USB, `hci0`)
- **Desktop** : Linux, 3 écrans dont une télé dédiée à l'affichage rameur
- **Homelab** : Proxmox + Home Assistant (objectif futur : onglet Santé)

## Caractéristiques FTMS confirmées

| UUID | Nom | Usage |
|------|-----|-------|
| `00002ad1` | Rower Data | Notify — flux principal des métriques |
| `00002acc` | Fitness Machine Feature | Read — capacités déclarées (`a656000004000000`) |
| `00002ad9` | Fitness Machine Control Point | Write/Indicate — commandes (résistance, start/stop) |
| `00002ada` | Fitness Machine Status | Notify — état machine |
| `00002ad3` | Training Status | Notify |

## Structure du projet

```
Domyos/
├── CLAUDE.md          ← ce fichier
├── ftms.py            ← protocole FTMS : UUIDs, opcodes, parse_rower_data, parse_features
├── logger.py          ← SessionLogger : SQLite (WAL, connexion unique, auto-détection de session)
├── read_rower.py      ← lecteur BLE terminal (bleak) — importe ftms + logger
├── scan_rower.py      ← dump des services/characteristics GATT
├── domyos.db          ← base SQLite (sessions + datapoints)
├── server.py          ← process central : BLE (reconnexion auto) + WS + API REST sur :8765
├── ha.py              ← Home Assistant : MQTT Discovery + état 1 Hz + webhook fin de séance
├── config.py          ← secrets locaux (MQTT, webhook) — NON versionné ; modèle : config.example.py
├── .gitignore         ← __pycache__, *.db*, config.py, .env, *.odt
├── dashboard/
│   └── index.html     ← dashboard TV (HTML/Canvas2D, sans dépendance), servi sur http://localhost:8765/
└── trainer.py         ← programmes d'entraînement : client WS de server.py, pilote la résistance
```

## État actuel

- ✅ Scan BLE → détection `Domyos-ROW-0743`
- ✅ Connexion + lecture GATT (services et caractéristiques)
- ✅ Parsing FTMS `0x2AD1` : temps, cadence, coups, distance, allure, puissance, calories, FC, résistance
- ✅ Affichage terminal temps réel (redraw propre sans scroll)
- ✅ Logging SQLite : table `sessions` (agrégats) + table `datapoints` (~4 points/s)
- ✅ Session test : 88s | 238m | 30 coups | 56W moy | 374 datapoints
- ✅ `ftms.py` extrait (parser + features + opcodes Control Point) ; features décodées :
  cadence, distance, pace, resistance_level, expended_energy, heart_rate, elapsed_time,
  power_measurement — cible supportée : **resistance_target**
- ✅ Logger v2 : connexion unique + WAL, commit/1s, session démarrée au premier coup et
  clôturée sur reset machine ou 30 s sans progression du chrono (`record()` renvoie
  `"started"` / `"stopped"` pour que `server.py` puisse broadcaster l'événement)
- ✅ Sessions fantômes corrigées (14/10) : au reset, le rameur remet le chrono à zéro un
  ou deux paquets **avant** le compteur de coups ; ce compteur résiduel n'ouvre plus de
  session, et toute session clôturée sans coup compté (`min_strokes=1`) est supprimée
  avec ses datapoints (`server.py` ne broadcaste alors pas de `session/stopped`)
- ✅ `server.py` (aiohttp, un seul port 8765) : `/ws` temps réel, `/api/state`,
  `/api/sessions`, `/api/sessions/{id}` ; reconnexion BLE toutes les 5 s quand le rameur
  est éteint ; commandes Control Point reçues par WS (`set_resistance`, `start`, `pause`,
  `stop`, `reset`) — **pilotage non encore validé sur le rameur** (voir
  `ftms.RESISTANCE_ENCODING`)
- ✅ Validé en réel (session #2, 51 s / 142 m) : reconnexion auto, `started_or_resumed`
  au 1er coup, `paused` depuis l'écran
- ✅ Pause / coasting / machine_state : `paused` suspend le timeout de session (filet de
  sécurité 5 min hors pause) ; datapoints `coasting=1` quand **ni** le compteur de coups
  **ni** le triplet (puissance, allure, cadence) n'ont bougé depuis > 5 s (exclus des
  moyennes, distance et chrono conservés) ; `status.machine_state`
  ∈ offline / idle / running / paused. Règle affinée le 14/10 : à la reprise après un
  arrêt court, la console incrémente le compteur de coups avec ~10 s de retard alors que
  la puissance varie déjà (session 14 : 73 W max exclu à tort avec l'ancienne règle).
- ✅ Dashboard TV v1 (`dashboard/index.html`) : 4 grandes tuiles (temps, distance, allure,
  puissance), 4 petites (cadence, coups, calories, moyenne séance), 2 graphes 60 s
  (puissance, cadence, un axe chacun), bandeaux plein écran PAUSE / PRÊT / HORS LIGNE,
  tuiles grisées « figé » pendant le coasting, reconnexion WS auto, curseur masqué
- ✅ `python3 server.py --demo` : rameur simulé (séance 90 s → figé 15 s → pause 20 s →
  reset, en boucle), sessions écrites dans `demo.db` — pour développer le dashboard
  sans ramer ; `--port N` pour tourner à côté du serveur réel
- ✅ `trainer.py` : `probe` (valide l'encodage sur 1/8/15), `set N`,
  `run intervals|pyramid|steady|fichier.json [--no-send]`, `stop`. Les étapes suivent le
  chrono du rameur (une pause le met en pause). Le serveur relaie `{"type":"program"}` ;
  le dashboard affiche le bandeau (étape, **cible R**, temps restant, barre) et, si la
  molette n'est pas sur la cible, « → règle la molette sur N » clignotant + bip.
- ✅ **Étape 4 — Home Assistant (14/10)** : `ha.py`, branché dans `server.py` via
  `broadcast()` (tout message WS passe aussi par `ha.handle()`). MQTT Discovery
  *device-based* (`homeassistant/device/domyos_woodrower/config`, retain) → device
  « Rameur » avec 22 entités : live (`sensor.rameur_puissance`, `_cadence`, `_coups`,
  `_distance`, `_allure`, `_allure_500_m`, `_calories`, `_temps_ecoule`, `_resistance`,
  `_frequence_cardiaque`), état (`sensor.rameur_etat`, `binary_sensor.rameur_en_seance`,
  `binary_sensor.rameur_connecte`) et dernière séance (`sensor.rameur_derniere_seance_*`,
  retain). Topics `domyos/woodrower/{availability,status,state,last_session}` ; `state`
  publié à 1 Hz ; `distance`/`calories`/`coups` en `total_increasing` → statistiques long
  terme HA (totaux jour/semaine). Webhook POST JSON `{event:"session_stopped", demo,
  device, session:{…ligne sessions…}}` à chaque clôture (3 tentatives). Validé en démo
  (`--demo --ha` → device séparé « Rameur démo », topics `domyos/demo/*`) : 1 Hz, retain,
  webhook HTTP 200. Sans `config.py` ou avec `--no-ha` : désactivé.
  `status.ha_connected` exposé dans l'API/WS. `python3 ha.py cleanup-demo` retire le
  device démo de HA.
- ✅ **Anti-veille (14/10)** : l'écran du dashboard (3ᵉ écran PC, GNOME idle-delay 5 min)
  se mettait en veille en séance. Deux couches : Screen Wake Lock dans le dashboard tant
  que serveur et rameur sont joignables, et `ScreenInhibitor` dans `server.py`
  (`gnome-session-inhibit --inhibit idle --inhibit-only`, lancé quand le rameur se
  connecte, tué quand il passe hors ligne, PR_SET_PDEATHSIG pour ne jamais laisser
  d'orphelin). `--no-inhibit` pour désactiver. `window.domyosDebug()` dans la console du
  dashboard montre l'état du verrou.
- ⚠️ **La résistance du Woodrower n'est pas motorisée.** Validé en réel : la console
  accepte Set Target Resistance à l'arrêt (indication `success`, niveau reflété dans
  Rower Data) mais le refuse en séance (`invalid_parameter`) et l'effort ne change
  jamais. Le niveau vient de la molette. Les programmes sont donc des **consignes
  affichées**, comme Kinomap. Encodage spec (UINT8 × 10) conservé dans `ftms.py`.

## Session du 14/10/2026 — bilan

- ✅ Sessions fantômes corrigées, coasting affiné (voir « État actuel »).
- ✅ Étape 4 validée en réel (sessions 14, 15, 16) : device « Rameur » dans HA, live 1 Hz,
  statut idle/running/paused/offline, `last_session` retenu, webhook → automation HA avec
  notification iPhone (stats de la séance) + compteur « Séances totales ». Vue « Santé »
  créée par Cowork dans le dashboard « Maison » (cartes Rameur / Dernière séance /
  Historique ApexCharts — les cartes Apex affichent encore « Erreur de configuration »,
  à revoir côté YAML avec Cowork).
- Session 17 (5 min, 766 m) avec le dashboard en kiosque Brave sur le 3ᵉ écran : OK, mais
  `trainer.py run intervals …` n'a jamais atteint le serveur : lancé avant la connexion
  BLE, il quittait aussitôt avec « Le rameur n'est pas connecté ». Corrigé : il attend
  désormais le rameur (« En attente du rameur… »), puis le premier coup.
- ✅ **Session 18 (6 min 15 s, 947 m) avec programme intervals 60/30×4/60 `--no-send`** :
  les 10 étapes se sont enchaînées à la seconde sur le chrono du rameur, molette suivie
  de 2 à 5 (cible atteinte en 4 à 11 s), anti-veille actif puis relâché à l'extinction,
  HA alimenté, trainer déconnecté proprement. Puissance moyenne par niveau : R2 ≈ 41 W,
  R5 ≈ 51 W. Corrigé au passage : le trainer n'envoie plus de consigne finale avec
  `--no-send`.
- 📌 **Découverte** : la console notifie chaque cran de molette via Fitness Machine
  Status `0x07` (target_resistance_changed, param = niveau × 10) — plus réactif que le
  champ résistance du flux Rower Data pour l'alerte molette du dashboard.

## Prochaine session

1. Retour session 18 : écran lisible, **bip jamais entendu**. Bip refait (14/10 soir) :
   carré 880 Hz × 2 à la montée / 440 Hz long à la descente, gain 0,4 ; indicateur
   « 🔇 son bloqué » dans la barre si l'AudioContext est suspendu (flag autoplay absent),
   débloqué par un clic ; **touche B = bip de test** en kiosque. À vérifier en conditions :
   appuyer sur B dans le kiosque, puis au besoin contrôler la sortie son choisie pour
   Brave (le 3ᵉ écran HDMI peut capter la sortie audio).
2. Côté HA (Cowork) : corriger les cartes ApexCharts de la vue « Santé » ;
   `python3 ha.py cleanup-demo` pour retirer le device « Rameur démo ».
3. Hygiène : `git init` (`.gitignore` et `config.example.py` existent déjà) ; sortir
   `LibreOffice Writer.odt` du dossier projet (il contient le mot de passe MQTT).
4. Optionnel : recalculer les agrégats de la session 14 avec la règle de coasting affinée
   (moy. 43,9 W, max 73 W, allure 200 s, cadence 21,7) — sessions 15 et 16 inchangées.

Points ouverts :
- Bip du dashboard : nécessite `--autoplay-policy=no-user-gesture-required` sur Brave
  (ou un clic dans la page), non testé sur la télé.
- Pas de capteur FC : si un jour ceinture BLE (service 0x180D), l'intégrer dans `server.py`.

## Comportement observé du rameur

- Émet à 4 Hz dès qu'il est allumé, tout à zéro tant qu'on ne rame pas.
- Fitness Machine Status : `04` (started) au premier coup, `02 02` (paused) ~15 s après
  le dernier coup ; l'écran permet pause / reset, la molette règle la résistance.
- **Pendant ces ~15 s il fige puissance / allure / cadence sur leurs dernières valeurs
  tout en faisant avancer chrono et distance** (d'où le flag `coasting`).
- En pause : chrono figé, valeurs à zéro, émission continue. Il coupe le BLE après
  extinction → reconnexion gérée par `server.py`.
- Pas de capteur FC (toujours 0) → prévoir une ceinture BLE (service 0x180D) si besoin.

## Base de données SQLite

```sql
sessions   : id, started_at, ended_at, duration_s, distance_m, strokes,
             calories, avg_power_w, avg_pace_s, avg_stroke_rate,
             max_power_w, resistance_used (JSON)

datapoints : id, session_id, ts, elapsed_s, stroke_rate, stroke_count,
             distance_m, pace_s, power_w, calories, heart_rate, resistance,
             coasting (1 = valeurs figées par le rameur, exclues des moyennes)
```

---

## Plan de développement — Architecture cible

L'objectif est un **process Python central** (`server.py`) qui concentre la connexion BLE et distribue les données à tous les consommateurs via WebSocket.

### Étape 1 — Server central + WebSocket — ✅ fait

`server.py` remplace `read_rower.py` comme point d'entrée unique :

```
BleakClient (BLE notify)
        │
        ▼
   server.py  ──── SessionLogger (SQLite)
        │
        ▼
  WebSocket (asyncio + websockets lib)
        │
   ┌────┴────┐
   │         │
dashboard  futur client HA / trainer
(browser)
```

- Librairie : `websockets` (async, compatible avec la boucle bleak)
- Le serveur écoute sur `ws://localhost:8765`
- Chaque paquet BLE parsé → broadcasté en JSON à tous les clients connectés
- Le terminal garde un affichage minimal (connexion, nb clients, Ctrl+C)

**Format JSON émis par WebSocket :**
```json
{
  "ts": "2026-10-13T19:00:00Z",
  "elapsed_s": 88,
  "stroke_rate": 23.0,
  "stroke_count": 30,
  "distance_m": 238,
  "pace_s": 170,
  "power_w": 56,
  "calories": 6,
  "heart_rate": 0,
  "resistance": 4
}
```

### Étape 2 — Dashboard TV (HTML/JS) — ✅ fait (v1)

Page HTML fullscreen servie par un petit serveur HTTP (`aiohttp` ou `http.server`), ouverte dans le navigateur sur la télé :

- Se connecte au WebSocket de `server.py`
- Affichage temps réel : grandes métriques (temps, distance, allure, puissance)
- Graphe live cadence/puissance sur les 60 dernières secondes (Chart.js ou Canvas2D)
- Thème sombre adapté à l'affichage TV (grande police, fort contraste)

### Étape 3 — Pilotage résistance (Control Point `0x2AD9`) — ✅ fait, en mode consigne (résistance manuelle)

Commandes FTMS à implémenter dans `trainer.py` :

| OpCode | Commande | Paramètre |
|--------|----------|-----------|
| `0x01` | Reset | — |
| `0x04` | Set Target Resistance Level | UINT8 = niveau × 10 (spec FTMS, résolution 0,1) |
| `0x07` | Start / Resume | — |
| `0x08` | Stop / Pause | `0x01` = stop, `0x02` = pause |

Permet : intervalles automatiques, pyramides, programmes FC-cible.

### Étape 4 — Intégration Home Assistant — ✅ fait (14/10), détails dans « État actuel »

Côté HA, **MQTT (broker) et un webhook sont déjà en place et fonctionnels**.

Plan proposé (à valider le 14/10) :
- `ha.py` (ou module importé par `server.py`) :
  - **MQTT live** : publish des métriques à cadence réduite (1 Hz, pas 4 Hz) sur
    `domyos/rower/state` (JSON) + `domyos/rower/status` (online/offline, machine_state) ;
    MQTT Discovery pour créer automatiquement les capteurs
    (`sensor.rameur_puissance`, `sensor.rameur_distance`, `sensor.rameur_cadence`,
    `sensor.rameur_allure`, `binary_sensor.rameur_en_seance`, …)
  - **Webhook fin de session** : POST des agrégats de `sessions` (JSON du message
    `session/stopped`) → automation HA qui alimente l'historique / l'onglet Santé
  - `retain` sur status et dernière session ; reconnexion MQTT auto
- Config (host, port, user/pass MQTT, URL webhook) dans un `config.py` ou `.env`
  **non versionné** (créer `.gitignore` : `__pycache__/`, `*.db*`, `demo.db*`, `.env`)
- Librairie : `aiomqtt` (async, compatible boucle bleak/aiohttp)

Objectif : onglet Santé dans HA avec historique des sessions, courbes de progression,
capteurs (`sensor.rameur_distance_totale`, `sensor.rameur_calories_semaine`, etc.)

## Commandes utiles

```bash
# Lancer le serveur central (BLE + WebSocket + API + dashboard)
python3 server.py
python3 server.py --demo          # rameur simulé, DB demo.db
curl localhost:8765/api/state

# Dashboard sur la télé (plein écran, kiosque)
brave-browser --kiosk --app=http://localhost:8765/ --autoplay-policy=no-user-gesture-required   # F11 si la barre de titre reste ; autoplay pour le bip

# Pilotage de la résistance (serveur lancé, rameur allumé)
python3 trainer.py probe                     # 1re fois : trouver l'encodage → ftms.RESISTANCE_ENCODING
python3 trainer.py set 5
python3 trainer.py run intervals --work 60 --rest 60 --reps 6 --hi 8 --lo 3
python3 trainer.py run pyramid --lo 2 --hi 8 --step 60
python3 trainer.py stop

# Home Assistant
python3 server.py --no-ha                  # sans MQTT ni webhook
python3 server.py --demo --ha              # démo publiée sur un device séparé « Rameur démo »
python3 ha.py cleanup-demo                 # retire le device démo de HA (payloads vides retenus)

# Lecteur terminal seul (sans serveur)
python3 read_rower.py

# Vérifier la DB
sqlite3 domyos.db "SELECT id, started_at, duration_s, distance_m, avg_power_w, calories FROM sessions;"
sqlite3 domyos.db "SELECT COUNT(*) FROM datapoints WHERE session_id=1;"

# Scanner le rameur
bluetoothctl scan on   # chercher Domyos-ROW-0743

# État du dongle BT
hciconfig -a
systemctl status bluetooth
```

## Dépendances Python

```bash
pip install bleak --break-system-packages        # BLE (3.0.2)
pip install aiohttp --break-system-packages      # serveur HTTP/WS + client trainer (3.14.5)
pip install aiomqtt --break-system-packages      # Home Assistant (2.5.1, paho-mqtt 2.1.0)
```
`websockets` (10.4, vieille API) n'est plus utilisé : tout passe par aiohttp.
