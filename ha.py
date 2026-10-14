"""
Domyos Woodrower — Intégration Home Assistant

- MQTT (aiomqtt, reconnexion auto) :
    <prefix>/availability   online/offline (retain + LWT) — le process server.py
    <prefix>/status         JSON status du serveur (retain) : connected, machine_state…
    <prefix>/state          JSON du dernier paquet, publié à 1 Hz (pas à 4 Hz)
    <prefix>/last_session   JSON des agrégats de la dernière séance (retain)
    <discovery>/device/<device_id>/config   MQTT Discovery (device-based, retain)
      → capteurs live (sensor.rameur_puissance, _distance, _cadence, _allure…),
        binary_sensor.rameur_en_seance / _connecte, sensor.rameur_etat,
        et sensor.rameur_derniere_seance_* qui survivent aux redémarrages (retain)
- Webhook HA : POST JSON des agrégats à chaque fin de séance (3 tentatives)

Utilisation (depuis server.py) :
    ha = HomeAssistant(config)         # ou HomeAssistant(config, demo=True)
    await ha.start()
    ha.handle(msg)                     # chaque message WS broadcasté (data/status/session)
    await ha.stop()
"""

import asyncio
import json
from datetime import datetime

import aiohttp
import aiomqtt

MQTT_RETRY_S = 5
WEBHOOK_RETRIES_S = (0, 3, 10)   # délais avant chaque tentative


def _log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [HA] {msg}", flush=True)


class HomeAssistant:
    def __init__(self, cfg, demo: bool = False):
        self.host = cfg.MQTT_HOST
        self.port = cfg.MQTT_PORT
        self.user = cfg.MQTT_USER
        self.password = cfg.MQTT_PASSWORD
        self.live_interval = getattr(cfg, "MQTT_LIVE_INTERVAL_S", 1.0)
        self.webhook_url = getattr(cfg, "HA_WEBHOOK_URL", None)
        self.demo = demo

        discovery = getattr(cfg, "MQTT_DISCOVERY_PREFIX", "homeassistant")
        prefix = getattr(cfg, "MQTT_TOPIC_PREFIX", "domyos/woodrower")
        if demo:
            prefix = "domyos/demo"
        self.device_id = "domyos_demo" if demo else "domyos_woodrower"
        self.device_name = "Rameur démo" if demo else "Rameur"
        self.t_avail = f"{prefix}/availability"
        self.t_status = f"{prefix}/status"
        self.t_state = f"{prefix}/state"
        self.t_last = f"{prefix}/last_session"
        self.t_config = f"{discovery}/device/{self.device_id}/config"

        self.connected = False
        self._latest: dict | None = None        # dernier paquet data, publié à 1 Hz
        self._status: dict | None = None        # dernier status (republié à la reconnexion)
        self._outbox: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._webhook_tasks: set[asyncio.Task] = set()

    # ── API publique ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._task = asyncio.create_task(self._mqtt_loop())
        _log(f"MQTT {self.host}:{self.port} → {self.t_state} ({self.device_name})")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for t in list(self._webhook_tasks):
            t.cancel()
        # Dernier souffle propre (le LWT couvre le cas d'un crash)
        try:
            async with aiomqtt.Client(self.host, self.port, username=self.user,
                                      password=self.password, timeout=3) as client:
                await client.publish(self.t_avail, "offline", retain=True)
        except (aiomqtt.MqttError, OSError, asyncio.TimeoutError):
            pass

    def handle(self, msg: dict) -> None:
        """Appelé par server.py pour chaque message broadcasté aux clients WS."""
        t = msg.get("type")
        if t == "data":
            self._latest = msg
        elif t == "status":
            self._status = msg
            self._outbox.put_nowait((self.t_status, self._status_payload(msg), True))
        elif t == "session" and msg.get("event") == "stopped" and msg.get("stats"):
            stats = dict(msg["stats"])
            ru = stats.get("resistance_used")
            if isinstance(ru, str):
                try:
                    stats["resistance_used"] = json.loads(ru)
                except ValueError:
                    pass
            self._outbox.put_nowait((self.t_last, json.dumps(stats), True))
            if self.webhook_url:
                task = asyncio.create_task(self._post_webhook(stats))
                self._webhook_tasks.add(task)
                task.add_done_callback(self._webhook_tasks.discard)

    # ── MQTT ──────────────────────────────────────────────────────────────────
    def _status_payload(self, status: dict) -> str:
        return json.dumps({
            "connected":     bool(status.get("connected")),
            "machine_state": status.get("machine_state", "offline"),
            "session_id":    status.get("session_id"),
            "demo":          self.demo,
        })

    async def _mqtt_loop(self) -> None:
        announced = False
        while True:
            try:
                async with aiomqtt.Client(
                    self.host, self.port, username=self.user, password=self.password,
                    identifier=f"{self.device_id}-server", timeout=10,
                    will=aiomqtt.Will(self.t_avail, "offline", retain=True),
                ) as client:
                    self.connected = True
                    announced = False
                    _log("connecté au broker")
                    await client.publish(self.t_config, json.dumps(self._discovery()), retain=True)
                    await client.publish(self.t_avail, "online", retain=True)
                    if self._status:
                        await client.publish(self.t_status, self._status_payload(self._status), retain=True)
                    pump = asyncio.create_task(self._pump_outbox(client))
                    try:
                        await self._publish_live(client)
                    finally:
                        pump.cancel()
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError, asyncio.TimeoutError) as e:
                if not announced:
                    _log(f"broker injoignable ({e}), nouvel essai toutes les {MQTT_RETRY_S}s")
                    announced = True
            finally:
                self.connected = False
            await asyncio.sleep(MQTT_RETRY_S)

    async def _pump_outbox(self, client: aiomqtt.Client) -> None:
        while True:
            topic, payload, retain = await self._outbox.get()
            await client.publish(topic, payload, retain=retain)

    async def _publish_live(self, client: aiomqtt.Client) -> None:
        last_sent = None
        while True:
            await asyncio.sleep(self.live_interval)
            if self._latest is not None and self._latest is not last_sent:
                last_sent = self._latest
                await client.publish(self.t_state, json.dumps(self._latest))

    # ── Webhook ───────────────────────────────────────────────────────────────
    async def _post_webhook(self, stats: dict) -> None:
        payload = {"event": "session_stopped", "demo": self.demo,
                   "device": self.device_id, "session": stats}
        timeout = aiohttp.ClientTimeout(total=10)
        for i, delay in enumerate(WEBHOOK_RETRIES_S, 1):
            await asyncio.sleep(delay)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.post(self.webhook_url, json=payload) as r:
                        if r.status < 300:
                            _log(f"webhook OK (session #{stats.get('id')}, HTTP {r.status})")
                            return
                        _log(f"webhook HTTP {r.status} (tentative {i})")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                _log(f"webhook échec ({e.__class__.__name__}, tentative {i})")
        _log(f"webhook abandonné pour la session #{stats.get('id')}")

    # ── MQTT Discovery ────────────────────────────────────────────────────────
    def _discovery(self) -> dict:
        avail_server = [{"topic": self.t_avail}]
        avail_rower = avail_server + [{
            "topic": self.t_status,
            "value_template": "{{ 'online' if value_json.connected else 'offline' }}",
        }]
        cmps: dict[str, dict] = {}

        def sensor(key, name, template, *, topic, unit=None, device_class=None,
                   state_class=None, icon=None, availability=None, platform="sensor",
                   **extra):
            c = {"p": platform, "name": name,
                 "unique_id": f"{self.device_id}_{key}",
                 "state_topic": topic, "value_template": template}
            if unit:         c["unit_of_measurement"] = unit
            if device_class: c["device_class"] = device_class
            if state_class:  c["state_class"] = state_class
            if icon:         c["icon"] = icon
            if availability:
                c["availability"] = availability
                c["availability_mode"] = "all"
            c.update(extra)
            cmps[key] = c

        # Métriques live (1 Hz) — indisponibles quand le rameur est éteint
        live = dict(topic=self.t_state, availability=avail_rower)
        sensor("puissance", "Puissance", "{{ value_json.power_w | int(0) }}",
               unit="W", device_class="power", state_class="measurement", **live)
        sensor("cadence", "Cadence", "{{ value_json.stroke_rate | float(0) }}",
               unit="spm", state_class="measurement", icon="mdi:rowing", **live)
        sensor("coups", "Coups", "{{ value_json.stroke_count | int(0) }}",
               state_class="total_increasing", icon="mdi:counter", **live)
        sensor("distance", "Distance", "{{ value_json.distance_m | int(0) }}",
               unit="m", device_class="distance", state_class="total_increasing", **live)
        sensor("allure", "Allure", "{{ value_json.pace_s | int(0) }}",
               unit="s", icon="mdi:speedometer", state_class="measurement", **live)
        sensor("allure_texte", "Allure /500 m",
               "{% set p = value_json.pace_s | int(0) %}{{ '%d:%02d' % (p // 60, p % 60) }}",
               icon="mdi:speedometer", **live)
        sensor("calories", "Calories", "{{ value_json.calories | int(0) }}",
               unit="kcal", state_class="total_increasing", icon="mdi:fire", **live)
        sensor("temps", "Temps écoulé", "{{ value_json.elapsed_s | int(0) }}",
               unit="s", device_class="duration", icon="mdi:timer-outline", **live)
        sensor("resistance", "Résistance", "{{ value_json.resistance | int(0) }}",
               state_class="measurement", icon="mdi:tune", **live)
        sensor("frequence_cardiaque", "Fréquence cardiaque", "{{ value_json.heart_rate | int(0) }}",
               unit="bpm", state_class="measurement", icon="mdi:heart-pulse", **live)

        # État du serveur / de la machine — indisponibles quand server.py est arrêté
        st = dict(topic=self.t_status, availability=avail_server)
        sensor("etat", "État", "{{ value_json.machine_state }}", device_class="enum",
               options=["offline", "idle", "running", "paused"], icon="mdi:state-machine", **st)
        sensor("en_seance", "En séance",
               "{{ 'ON' if value_json.machine_state in ['running', 'paused'] else 'OFF' }}",
               platform="binary_sensor", device_class="running", **st)
        sensor("connecte", "Connecté", "{{ 'ON' if value_json.connected else 'OFF' }}",
               platform="binary_sensor", device_class="connectivity", **st)

        # Dernière séance (retain → conservée après redémarrage de HA ou du serveur)
        last = dict(topic=self.t_last)
        sensor("derniere_seance_distance", "Dernière séance distance",
               "{{ value_json.distance_m | int(0) }}", unit="m", device_class="distance",
               icon="mdi:map-marker-distance", **last)
        sensor("derniere_seance_duree", "Dernière séance durée",
               "{{ value_json.duration_s | int(0) }}", unit="s", device_class="duration",
               icon="mdi:timer-outline", **last)
        sensor("derniere_seance_puissance", "Dernière séance puissance moyenne",
               "{{ value_json.avg_power_w | float(0) }}", unit="W", device_class="power", **last)
        sensor("derniere_seance_puissance_max", "Dernière séance puissance max",
               "{{ value_json.max_power_w | int(0) }}", unit="W", device_class="power", **last)
        sensor("derniere_seance_calories", "Dernière séance calories",
               "{{ value_json.calories | int(0) }}", unit="kcal", icon="mdi:fire", **last)
        sensor("derniere_seance_coups", "Dernière séance coups",
               "{{ value_json.strokes | int(0) }}", icon="mdi:counter", **last)
        sensor("derniere_seance_cadence", "Dernière séance cadence moyenne",
               "{{ value_json.avg_stroke_rate | float(0) }}", unit="spm", icon="mdi:rowing", **last)
        sensor("derniere_seance_allure", "Dernière séance allure moyenne",
               "{% set p = value_json.avg_pace_s | int(0) %}{{ '%d:%02d' % (p // 60, p % 60) }}",
               icon="mdi:speedometer", **last)
        sensor("derniere_seance_fin", "Dernière séance fin",
               "{{ value_json.ended_at }}", device_class="timestamp", **last)

        return {
            "dev": {
                "ids": self.device_id,
                "name": self.device_name,
                "mf": "Decathlon",
                "mdl": "Domyos Woodrower",
                "sw": "domyos-server",
                **({} if self.demo else {"cns": [["bluetooth", "1C:2A:85:46:23:5A"]]}),
            },
            "o": {"name": "domyos-server"},
            "cmps": cmps,
        }


async def cleanup_demo(cfg) -> None:
    """Retire le device « Rameur démo » de HA : payloads vides retenus sur ses topics."""
    ha = HomeAssistant(cfg, demo=True)
    async with aiomqtt.Client(ha.host, ha.port, username=ha.user, password=ha.password) as c:
        for t in (ha.t_config, ha.t_avail, ha.t_status, ha.t_last):
            await c.publish(t, b"", retain=True)
    _log("device démo retiré")


if __name__ == "__main__":
    # python3 ha.py cleanup-demo  → supprime le device « Rameur démo » créé par --demo --ha
    import sys
    import config
    if sys.argv[1:] == ["cleanup-demo"]:
        asyncio.run(cleanup_demo(config))
    else:
        print("usage : python3 ha.py cleanup-demo")
