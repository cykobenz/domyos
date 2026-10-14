"""
Domyos Woodrower — Serveur central

- Garde la connexion BLE au rameur (reconnexion automatique quand il
  s'éteint / se rallume)
- Logge les sessions en SQLite (logger.SessionLogger)
- Expose sur http://localhost:8765 :
    /ws                      WebSocket temps réel (JSON)
    /                        dashboard/ (si le dossier existe)
    /api/state               état courant + dernier paquet
    /api/sessions            sessions terminées (?limit=20)
    /api/sessions/{id}       une session + ses datapoints

Messages WebSocket serveur → clients :
    {"type":"data", ts, elapsed_s, stroke_rate, stroke_count, distance_m,
                    pace_s, power_w, calories, heart_rate, resistance, session_id,
                    coasting (true = valeurs figées par le rameur après le dernier coup)}
    {"type":"status", connected, session_id, clients,
                      machine_state: "offline"|"idle"|"running"|"paused"}
    {"type":"session", event:"started"|"stopped", session_id, stats}
    {"type":"machine_status", code, name, param}
    {"type":"control_response", request_opcode, result}

Messages clients → serveur (pilotage, étape 3) :
    {"type":"set_resistance", "level": 1..15, "encoding": "spec"|"uint8"|"int16" (optionnel)}
    {"type":"start"} | {"type":"pause"} | {"type":"stop"} | {"type":"reset"}
    {"type":"program_start", "preset": "<id de programs.json>"}
      ou {"type":"program_start", "name": "...", "kind": "intervals|pyramid|steady|steps",
          "params": {...} | "steps": [...]}   → le serveur déroule le programme sur le
          chrono du rameur (ProgramRunner) et diffuse {"type":"program", ...} à chaque
          seconde / changement d'étape ; {"type":"program","active":false,"finished":bool}
          à la fin ou sur {"type":"program_stop"}
    GET /api/programs : presets de programs.json ; POST (ajout/remplacement) ; DELETE /api/programs/{id}
    dashboard/control.html : page de contrôle (bureau) — compose, arme et arrête les programmes
    Vidéo Jellyfin (video.py, si JELLYFIN_URL dans config.py) : {"type":"video_play", item_id}
      / video_pause / video_resume / video_stop / video_seek / video_volume ; le serveur
      diffuse {"type":"video", …} ; GET /api/video/{home,search,episodes/{id}} ; proxy /jf/…
"""

import asyncio
import json
import math
import random
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web, WSMsgType
from bleak import BleakClient
from bleak.exc import BleakError

import ftms
import programs
from logger import SessionLogger, DB_PATH

try:
    import config as cfg          # secrets locaux (voir config.example.py)
except ImportError:
    cfg = None

HOST = "0.0.0.0"
PORT = 8765
BLE_RETRY_S = 5
BLE_CONNECT_TIMEOUT_S = 10
DASHBOARD_DIR = Path(__file__).parent / "dashboard"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(tag: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [{tag}] {msg}", flush=True)


class ScreenInhibitor:
    """
    Empêche la mise en veille de l'écran tant que le rameur est allumé
    (gnome-session-inhibit --inhibit-only, tué dès que le rameur passe hors ligne).
    Complète le Screen Wake Lock du dashboard. Désactivable avec --no-inhibit.
    Le processus enfant reçoit SIGTERM si server.py meurt (PR_SET_PDEATHSIG),
    pour ne jamais laisser un inhibiteur orphelin bloquer la veille.
    """

    @staticmethod
    def _die_with_parent() -> None:
        try:
            import ctypes, signal
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG = 1
        except Exception:
            pass

    def __init__(self):
        self.cmd = shutil.which("gnome-session-inhibit")
        self.proc: asyncio.subprocess.Process | None = None
        if not self.cmd:
            log("VEILLE", "gnome-session-inhibit introuvable : pas d'anti-veille côté système")

    async def set(self, active: bool) -> None:
        if not self.cmd:
            return
        if active and (self.proc is None or self.proc.returncode is not None):
            self.proc = await asyncio.create_subprocess_exec(
                self.cmd, "--inhibit", "idle", "--app-id", "domyos-server",
                "--reason", "Séance de rameur", "--inhibit-only",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=self._die_with_parent)
            log("VEILLE", "écran maintenu éveillé (rameur allumé)")
        elif not active and self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
            self.proc = None
            log("VEILLE", "anti-veille levé (rameur éteint)")


class ProgramRunner:
    """
    Déroule un programme (liste d'étapes label / duration_s / resistance) sur le
    chrono du rameur : démarre au premier coup, se fige en pause, repart de
    l'étape courante après un reset. Diffuse {"type":"program"} aux clients WS.
    La résistance n'est jamais envoyée au rameur (molette manuelle) : c'est une
    consigne affichée par le dashboard.
    """

    def __init__(self, server: "RowerServer"):
        self.server = server
        self.name: str | None = None
        self.steps: list[dict] = []
        self.idx = 0
        self.total = 0
        self.step_start: int | None = None    # chrono rameur au début de l'étape courante
        self.msg: dict | None = None          # dernier message diffusé (renvoyé aux nouveaux clients)
        self._last_key = None
        self._power: list[list[float]] = []   # par étape : [somme des puissances, nb de paquets] (hors coasting)

    @property
    def active(self) -> bool:
        return bool(self.steps)

    async def start(self, name: str, steps: list[dict], source: str = "ws") -> None:
        self.name, self.steps, self.idx = name, steps, 0
        self.total = sum(s["duration_s"] for s in steps)
        self.step_start = None
        self._last_key = None
        self._power = [[0.0, 0] for _ in steps]
        log("PROG", f"« {name} » : {len(steps)} étapes, {self.total // 60} min {self.total % 60:02d} s ({source})")
        await self._publish(steps[0]["duration_s"])
        if self.server.machine_state == "running" and self.server.last_data:
            await self.tick(self.server.last_data.get("elapsed_s"))

    async def stop(self, finished: bool = False) -> None:
        if not self.active:
            return
        name, self.name, self.steps, self.msg = self.name, None, [], None
        self.step_start = None
        log("PROG", f"« {name} » {'terminé' if finished else 'arrêté'}")
        await self.server.broadcast({"type": "program", "active": False, "finished": finished, "name": name})

    async def tick(self, elapsed: int | None, power: float | None = None, coasting: bool = False) -> None:
        """À chaque paquet Rower Data (ou à la reprise)."""
        if not self.active or elapsed is None:
            return
        if self.step_start is None:
            if self.server.machine_state != "running":
                return                        # en attente du premier coup
            self.step_start = elapsed
        if elapsed < self.step_start:         # reset du rameur → on repart de l'étape courante
            self.step_start = elapsed
        if power and not coasting and self.server.machine_state == "running":
            acc = self._power[self.idx]
            acc[0] += power; acc[1] += 1
            self._last_key = None             # la moyenne de l'étape change → republier
        step = self.steps[self.idx]
        remaining = step["duration_s"] - (elapsed - self.step_start)
        if remaining <= 0:
            self.idx += 1
            self.step_start = elapsed
            if self.idx >= len(self.steps):
                await self.stop(finished=True)
                return
            remaining = self.steps[self.idx]["duration_s"]
        await self._publish(remaining)

    async def _publish(self, remaining: float) -> None:
        step = self.steps[self.idx]
        remaining = max(0, round(remaining))
        key = (self.idx, remaining)
        if key == self._last_key:
            return
        self._last_key = key
        nxt = self.steps[self.idx + 1] if self.idx + 1 < len(self.steps) else None
        self.msg = {
            "type": "program", "active": True, "name": self.name,
            "step": self.idx + 1, "steps": len(self.steps),
            "label": step["label"], "resistance": step["resistance"],
            "remaining_s": remaining, "total_s": self.total,
            "elapsed_s": sum(s["duration_s"] for s in self.steps[:self.idx]) + step["duration_s"] - remaining,
            "started": self.step_start is not None,
            "next_label": nxt["label"] if nxt else None,
            "next_resistance": nxt["resistance"] if nxt else None,
            # plan complet + puissance moyenne obtenue par étape (None tant que rien n'est mesuré)
            "plan": self.steps,
            "step_power": [round(s / n) if n else None for s, n in self._power],
        }
        await self.server.broadcast(self.msg)


class RowerServer:
    def __init__(self, db_path: Path = DB_PATH, ha=None, inhibitor: ScreenInhibitor | None = None):
        self.db_path = db_path
        self.logger = SessionLogger(db_path)
        self.ha = ha                         # ha.HomeAssistant ou None
        self.inhibitor = inhibitor           # ScreenInhibitor ou None
        self.clients: set[web.WebSocketResponse] = set()
        self.ble: BleakClient | None = None
        self.connected = False
        self.control_acquired = False
        self.last_data: dict | None = None
        self.runner = ProgramRunner(self)    # programme en cours (dashboard / trainer.py)
        self.video = None                    # video.Video (Jellyfin) ou None
        self.demo_resistance = 4             # résistance du rameur simulé (--demo)
        self._waiting_announced = False
        self._paused_broadcast = False

    # ── Messages ──────────────────────────────────────────────────────────────
    @property
    def program(self) -> dict | None:
        return self.runner.msg

    @property
    def machine_state(self) -> str:
        if not self.connected:
            return "offline"
        if not self.logger.active:
            return "idle"
        return "paused" if self.logger.paused else "running"

    def status_msg(self) -> dict:
        return {
            "type": "status",
            "connected": self.connected,
            "session_id": self.logger.session_id,
            "machine_state": self.machine_state,
            "clients": len(self.clients),
            "ha_connected": self.ha.connected if self.ha else None,
            "video_enabled": bool(self.video and self.video.enabled),
        }

    def data_msg(self, p: dict, session_id: int | None) -> dict:
        return {
            "type":         "data",
            "ts":           now_iso(),
            "elapsed_s":    p.get("elapsed_s"),
            "stroke_rate":  p.get("stroke_rate_spm"),
            "stroke_count": p.get("stroke_count"),
            "distance_m":   p.get("distance_m"),
            "pace_s":       p.get("pace_s_per_500m"),
            "power_w":      p.get("power_w"),
            "calories":     p.get("energy_total_kcal"),
            "heart_rate":   p.get("heart_rate_bpm"),
            "resistance":   p.get("resistance"),
            "session_id":   session_id,
            "coasting":     self.logger.coasting,
        }

    async def broadcast(self, msg: dict) -> None:
        if self.ha:
            self.ha.handle(msg)
        if self.inhibitor and msg.get("type") == "status":
            await self.inhibitor.set(bool(msg.get("connected")))
        if not self.clients:
            return
        text = json.dumps(msg)
        results = await asyncio.gather(
            *(ws.send_str(text) for ws in list(self.clients)), return_exceptions=True)
        for ws, r in zip(list(self.clients), results):
            if isinstance(r, Exception):
                self.clients.discard(ws)

    async def session_event(self, event: str | None, stats: dict | None = None) -> None:
        if not event:
            return
        sid = self.logger.session_id if event == "started" else (stats or {}).get("id")
        if sid is not None and event == "stopped":
            stats = self._session_stats(sid) or stats   # ligne complète (started_at inclus)
        await self.broadcast({"type": "session", "event": event,
                              "session_id": sid, "stats": stats})
        await self.broadcast(self.status_msg())

    # ── BLE ───────────────────────────────────────────────────────────────────
    async def on_rower_data(self, _sender, data: bytearray) -> None:
        parsed = ftms.parse_rower_data(data)
        before = self.logger.session_id
        event = self.logger.record(parsed)
        if before is not None and self.logger.session_id != before:
            # record() a clôturé une session (timeout ou reset) ; stats déjà en base,
            # sauf si le logger l'a supprimée (aucun coup compté) → simple mise à jour du statut
            stats = self._session_stats(before)
            if stats:
                await self.session_event("stopped", stats)
            else:
                await self.broadcast(self.status_msg())
        if event == "started":
            await self.session_event("started")
        was_paused = self._paused_broadcast
        if was_paused and not self.logger.paused:
            # le chrono est reparti sans événement machine : repasser en running
            self._paused_broadcast = False
            await self.broadcast(self.status_msg())
        self.last_data = self.data_msg(parsed, self.logger.session_id)
        await self.broadcast(self.last_data)
        await self.runner.tick(parsed.get("elapsed_s"), parsed.get("power_w"), self.logger.coasting)

    async def on_machine_status(self, _sender, data: bytearray) -> None:
        st = ftms.parse_machine_status(data)
        log("BLE", f"machine status : {st['name']} ({st['raw']})")
        await self.broadcast({"type": "machine_status", **st})
        if st["name"] in ("reset", "stopped"):
            stats = self.logger.stop()
            if stats:
                await self.session_event("stopped", stats)
            else:
                await self.broadcast(self.status_msg())
        elif st["name"] == "paused":
            self.logger.paused = True
            self._paused_broadcast = True
            await self.broadcast(self.status_msg())
        elif st["name"] == "started_or_resumed" and self.logger.paused:
            self.logger.paused = False
            self._paused_broadcast = False
            await self.broadcast(self.status_msg())
        if st["code"] == 0xFF:
            self.control_acquired = False

    async def on_control_response(self, _sender, data: bytearray) -> None:
        resp = ftms.parse_control_response(data)
        log("BLE", f"control point : {resp}")
        await self.broadcast({"type": "control_response", **resp})

    async def ble_once(self) -> None:
        disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_disconnect(_client):
            loop.call_soon_threadsafe(disconnected.set)

        async with BleakClient(ftms.ROWER_MAC, disconnected_callback=on_disconnect,
                               timeout=BLE_CONNECT_TIMEOUT_S) as client:
            self.ble = client
            self.connected = True
            self._waiting_announced = False
            feat = ftms.parse_features(await client.read_gatt_char(ftms.FIT_FEATURE_UUID))
            log("BLE", f"connecté au {ftms.ROWER_NAME} — cibles : {feat['targets']}")
            await self.broadcast(self.status_msg())

            await client.start_notify(ftms.ROWER_DATA_UUID, self.on_rower_data)
            await client.start_notify(ftms.FIT_STATUS_UUID, self.on_machine_status)
            try:
                await client.start_notify(ftms.CONTROL_POINT_UUID, self.on_control_response)
            except BleakError as e:
                log("BLE", f"indications Control Point indisponibles : {e}")
            await disconnected.wait()

    async def ble_loop(self) -> None:
        while True:
            try:
                await self.ble_once()
                log("BLE", "rameur déconnecté")
            except asyncio.CancelledError:
                raise
            except (BleakError, asyncio.TimeoutError, OSError) as e:
                if not self._waiting_announced:
                    log("BLE", f"rameur injoignable ({e.__class__.__name__}), "
                               f"nouvel essai toutes les {BLE_RETRY_S}s")
                    self._waiting_announced = True
            finally:
                if self.connected:
                    self.connected = False
                    self.ble = None
                    self.control_acquired = False
                    stats = self.logger.stop()
                    if stats:
                        await self.session_event("stopped", stats)
                    else:
                        await self.broadcast(self.status_msg())
            await asyncio.sleep(BLE_RETRY_S)

    async def demo_loop(self) -> None:
        """Rameur simulé (--demo) : séance de 90 s, coasting, pause, reset, en boucle."""
        import struct
        flags = (ftms.FLAG_TOTAL_DISTANCE | ftms.FLAG_INST_PACE | ftms.FLAG_INST_POWER |
                 ftms.FLAG_RESISTANCE | ftms.FLAG_EXPENDED_ENERGY | ftms.FLAG_HEART_RATE |
                 ftms.FLAG_ELAPSED_TIME)
        def pkt(sr, sc, dist, pace, pw, res, kcal, el):
            return bytearray(struct.pack("<H", flags) + bytes([int(sr * 2)]) + struct.pack("<H", int(sc))
                             + int(dist).to_bytes(3, "little")
                             + struct.pack("<HhhHHB", int(pace), int(pw), res, int(kcal), 300, 5)
                             + bytes([0]) + struct.pack("<H", int(el)))
        self.connected = True
        log("DEMO", "rameur simulé — séance 90 s / figé 15 s / pause 20 s / reset, en boucle")
        await self.broadcast(self.status_msg())
        while True:
            el = dist = strokes = kcal = 0.0
            sr = pw = pace = 0
            phase = [("idle", 5), ("row", 90), ("coast", 15), ("pause", 20)]
            for name, dur in phase:
                if name == "pause":
                    await self.on_machine_status(None, bytearray([0x02, 0x02]))
                for i in range(dur * 4):
                    if name == "row":
                        if i == 0:
                            await self.on_machine_status(None, bytearray([0x04]))
                        t = el / 90
                        sr = 22 + 6 * math.sin(t * math.pi * 2) + random.uniform(-1, 1)
                        pw = max(30, 70 + 40 * math.sin(t * math.pi * 2 + 1) + random.uniform(-8, 8))
                        pace = 200 - pw * 0.6
                        if i % 4 == 0 and random.random() < sr / 60 * 4:
                            strokes += 1
                        el += 0.25; dist += 0.25 * 500 / pace; kcal = el / 12
                    elif name == "coast":
                        el += 0.25; dist += 0.25 * 500 / pace
                    elif name == "pause":
                        sr = pw = pace = 0
                    await self.on_rower_data(None, pkt(sr, strokes, dist, pace, pw,
                                                       self.demo_resistance, kcal, el))
                    await asyncio.sleep(0.25)
            await self.on_machine_status(None, bytearray([0x01]))

    # ── Pilotage (Control Point) ──────────────────────────────────────────────
    async def handle_command(self, msg: dict) -> dict:
        cmd = msg.get("type")
        if isinstance(cmd, str) and cmd.startswith("video_"):
            if not self.video:
                return {"type": "error", "error": "video_disabled", "command": cmd}
            return await self.video.handle_command(msg)
        if cmd == "program_start":
            try:
                if msg.get("preset"):
                    spec = programs.preset(msg["preset"])
                    if not spec:
                        return {"type": "error", "error": "unknown_preset", "command": cmd}
                else:
                    spec = {"type": msg.get("kind", "steps"), "name": msg.get("name"),
                            "params": msg.get("params"), "steps": msg.get("steps")}
                name, steps = programs.build(spec)
            except (ValueError, KeyError, TypeError) as e:
                return {"type": "error", "error": f"programme invalide : {e}", "command": cmd}
            await self.runner.start(name, steps, source=msg.get("source", "ws"))
            return {"type": "ack", "command": cmd, "name": name, "steps": len(steps),
                    "total_s": self.runner.total}
        if cmd == "program_stop" or (cmd == "program" and not msg.get("active", True)):
            await self.runner.stop()
            return {"type": "ack", "command": cmd}
        if cmd == "program":
            return {"type": "error", "error": "use program_start", "command": cmd}
        if not self.connected:
            return {"type": "error", "error": "rower_not_connected", "command": cmd}
        if self.ble is None:                             # mode --demo : on simule le Control Point
            if cmd == "set_resistance":
                self.demo_resistance = int(msg["level"])
            log("DEMO", f"commande {cmd} simulée")
            await self.broadcast({"type": "control_response", "request_opcode": 0x04,
                                  "result": "success", "raw": "800401"})
            return {"type": "ack", "command": cmd, "payload": "demo"}
        try:
            if cmd == "set_resistance":
                payload = ftms.set_resistance_payload(
                    int(msg["level"]), msg.get("encoding", ftms.RESISTANCE_ENCODING))
            elif cmd == "start":
                payload = bytes([ftms.OP_START_RESUME])
            elif cmd == "pause":
                payload = bytes([ftms.OP_STOP_PAUSE, ftms.STOP_PARAM_PAUSE])
            elif cmd == "stop":
                payload = bytes([ftms.OP_STOP_PAUSE, ftms.STOP_PARAM_STOP])
            elif cmd == "reset":
                payload = bytes([ftms.OP_RESET])
            else:
                return {"type": "error", "error": "unknown_command", "command": cmd}

            if not self.control_acquired:
                await self.ble.write_gatt_char(
                    ftms.CONTROL_POINT_UUID, bytes([ftms.OP_REQUEST_CONTROL]), response=True)
                self.control_acquired = True
            await self.ble.write_gatt_char(ftms.CONTROL_POINT_UUID, payload, response=True)
            log("CMD", f"{cmd} → {payload.hex()}")
            return {"type": "ack", "command": cmd, "payload": payload.hex()}
        except (BleakError, KeyError, ValueError) as e:
            log("CMD", f"{cmd} échoué : {e}")
            return {"type": "error", "error": str(e), "command": cmd}

    # ── HTTP / WebSocket ──────────────────────────────────────────────────────
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        log("WS", f"client connecté ({request.remote}) — {len(self.clients)} client(s)")
        try:
            await ws.send_json(self.status_msg())
            if self.last_data:
                await ws.send_json(self.last_data)
            if self.program:
                await ws.send_json(self.program)
            if self.video and self.video.state.get("active"):
                await ws.send_json(self.video.state)
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        reply = await self.handle_command(json.loads(msg.data))
                    except json.JSONDecodeError:
                        reply = {"type": "error", "error": "invalid_json"}
                    await ws.send_json(reply)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            log("WS", f"client parti — {len(self.clients)} client(s)")
        return ws

    async def api_programs(self, _request) -> web.Response:
        try:
            return web.json_response(programs.presets())
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return web.json_response({"error": f"programs.json invalide : {e}"}, status=500)

    async def api_program_save(self, request) -> web.Response:
        """POST {name, desc?, kind, params?|steps?} → ajoute (ou remplace par id) un preset."""
        try:
            body = await request.json()
            pid = programs.save_preset(body)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        log("PROG", f"preset « {pid} » enregistré dans programs.json")
        return web.json_response({"id": pid, "presets": programs.presets()})

    async def api_program_delete(self, request) -> web.Response:
        pid = request.match_info["id"]
        if not programs.delete_preset(pid):
            raise web.HTTPNotFound()
        log("PROG", f"preset « {pid} » supprimé")
        return web.json_response({"presets": programs.presets()})

    async def api_state(self, _request) -> web.Response:
        return web.json_response({**self.status_msg(), "last_data": self.last_data,
                                  "program": self.program,
                                  "video": self.video.state if self.video else None})

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _session_stats(self, session_id: int | None) -> dict | None:
        if session_id is None:
            return None
        with self._db() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    async def api_sessions(self, request) -> web.Response:
        limit = int(request.query.get("limit", 20))
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE ended_at IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return web.json_response([dict(r) for r in rows])

    async def api_session(self, request) -> web.Response:
        sid = int(request.match_info["id"])
        with self._db() as conn:
            s = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if not s:
                raise web.HTTPNotFound()
            dps = conn.execute(
                "SELECT ts, elapsed_s, stroke_rate, stroke_count, distance_m, pace_s, "
                "power_w, calories, heart_rate, resistance FROM datapoints "
                "WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
        return web.json_response({**dict(s), "datapoints": [dict(r) for r in dps]})

    async def index(self, _request) -> web.Response:
        if (DASHBOARD_DIR / "index.html").exists():
            return web.FileResponse(DASHBOARD_DIR / "index.html")
        return web.Response(text="Domyos server — WebSocket sur /ws, API sur /api/state\n")

    @staticmethod
    async def _no_cache(request, response):
        # Chromium garde sinon les pages du dashboard en cache heuristique (pas de Cache-Control)
        # → après une mise à jour, le kiosque / la fenêtre de contrôle affichaient l'ancienne version
        if not request.path.startswith("/api/") and request.path != "/ws":
            response.headers["Cache-Control"] = "no-cache"

    def build_app(self) -> web.Application:
        app = web.Application()
        app.on_response_prepare.append(self._no_cache)
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/api/state", self.api_state)
        app.router.add_get("/api/programs", self.api_programs)
        app.router.add_post("/api/programs", self.api_program_save)
        app.router.add_delete("/api/programs/{id}", self.api_program_delete)
        app.router.add_get("/api/sessions", self.api_sessions)
        app.router.add_get("/api/sessions/{id}", self.api_session)
        if self.video:
            self.video.add_routes(app)       # /api/video/* + proxy /jf/* (avant le statique)
        if DASHBOARD_DIR.is_dir():
            app.router.add_static("/", DASHBOARD_DIR)
        return app

    # ── Cycle de vie ──────────────────────────────────────────────────────────
    async def run(self, port: int = PORT) -> None:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        await web.TCPSite(runner, HOST, port).start()
        log("HTTP", f"http://localhost:{port}  (ws://localhost:{port}/ws) — Ctrl+C pour quitter")
        if self.ha:
            await self.ha.start()
        if self.video:
            await self.video.start()
        demo = "--demo" in sys.argv
        ble_task = asyncio.create_task(self.demo_loop() if demo else self.ble_loop())
        try:
            await ble_task
        finally:
            ble_task.cancel()
            for ws in list(self.clients):
                await ws.close()
            await runner.cleanup()
            self.logger.close()
            if self.ha:
                await self.ha.stop()
            if self.video:
                await self.video.stop()
            if self.inhibitor:
                await self.inhibitor.set(False)
            log("SRV", "arrêté")


if __name__ == "__main__":
    # --demo : rameur simulé, sessions écrites dans demo.db (pas dans domyos.db)
    # --port N : port HTTP/WS (défaut 8765)
    # --no-ha : sans Home Assistant ; --ha : HA aussi en démo (device séparé « Rameur démo »)
    # --no-inhibit : ne pas bloquer la mise en veille de l'écran quand le rameur est allumé
    # --no-video : sans Jellyfin (sinon activé dès que config.py définit JELLYFIN_URL + clé)
    demo = "--demo" in sys.argv
    db = Path(__file__).parent / "demo.db" if demo else DB_PATH
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
    ha = None
    ha_wanted = cfg is not None and getattr(cfg, "HA_ENABLED", True) and "--no-ha" not in sys.argv
    if ha_wanted and (not demo or "--ha" in sys.argv):
        from ha import HomeAssistant
        ha = HomeAssistant(cfg, demo=demo)
    elif cfg is None:
        log("HA", "config.py absent → intégration Home Assistant désactivée")
    inhibitor = None if "--no-inhibit" in sys.argv else ScreenInhibitor()
    server = RowerServer(db, ha, inhibitor)
    if "--no-video" not in sys.argv and cfg is not None and getattr(cfg, "JELLYFIN_URL", "") and getattr(cfg, "JELLYFIN_API_KEY", ""):
        from video import Video
        server.video = Video(cfg, server)
    try:
        asyncio.run(server.run(port))
    except KeyboardInterrupt:
        pass
