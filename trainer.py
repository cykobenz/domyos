"""
Domyos Woodrower — Programmes d'entraînement (pilotage de la résistance)

Client WebSocket de server.py : suit le chrono du rameur pour enchaîner les
étapes (une pause sur le rameur met donc le programme en pause) et publie la
consigne de résistance, relayée par le serveur au dashboard qui l'affiche en
grand avec un bip quand elle change.

Sur le Woodrower la résistance se règle à la molette : la console accepte la
commande FTMS Set Target Resistance à l'arrêt (et la reflète dans ses données)
mais la refuse en séance (`invalid_parameter`) et ne bouge jamais la résistance
mécanique. Le trainer envoie quand même la commande (inoffensive) mais c'est
la consigne à l'écran qui compte.

    python3 trainer.py probe                  # quel encodage de résistance accepte le rameur ?
    python3 trainer.py set 5                  # règle la résistance (option --encoding)
    python3 trainer.py run intervals          # --warmup 120 --work 60 --rest 60 --reps 6 --hi 8 --lo 3 --cooldown 120
    python3 trainer.py run pyramid            # --lo 2 --hi 8 --step 60
    python3 trainer.py run steady             # --level 5 --minutes 20
    python3 trainer.py run mon_programme.json # [{"label": "...", "duration_s": 60, "resistance": 5}, ...]
    python3 trainer.py stop                   # efface le programme affiché, résistance --lo
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from aiohttp import ClientSession, WSMsgType

import ftms

SERVER = "ws://localhost:8765/ws"
RESPONSE_TIMEOUT_S = 4.0


# ── Programmes intégrés ───────────────────────────────────────────────────────
def prog_intervals(a) -> list[dict]:
    steps = [{"label": "Échauffement", "duration_s": a.warmup, "resistance": a.lo}]
    for i in range(1, a.reps + 1):
        steps.append({"label": f"Effort {i}/{a.reps}", "duration_s": a.work, "resistance": a.hi})
        steps.append({"label": f"Récup {i}/{a.reps}", "duration_s": a.rest, "resistance": a.lo})
    steps.append({"label": "Retour au calme", "duration_s": a.cooldown, "resistance": a.lo})
    return steps


def prog_pyramid(a) -> list[dict]:
    levels = list(range(a.lo, a.hi + 1)) + list(range(a.hi - 1, a.lo - 1, -1))
    return [{"label": f"Palier {lv}", "duration_s": a.step, "resistance": lv} for lv in levels]


def prog_steady(a) -> list[dict]:
    return [{"label": f"Palier {a.level}", "duration_s": a.minutes * 60, "resistance": a.level}]


def load_program(name: str, a) -> tuple[str, list[dict]]:
    if name == "intervals":
        return "Intervalles", prog_intervals(a)
    if name == "pyramid":
        return "Pyramide", prog_pyramid(a)
    if name == "steady":
        return "Palier", prog_steady(a)
    path = Path(name)
    if path.suffix == ".json" and path.exists():
        data = json.loads(path.read_text())
        steps = data["steps"] if isinstance(data, dict) else data
        return (data.get("name", path.stem) if isinstance(data, dict) else path.stem), steps
    sys.exit(f"Programme inconnu : {name}")


# ── Client ────────────────────────────────────────────────────────────────────
class Trainer:
    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.state: dict = {}
        self.last: dict = {}
        self._queue: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        self._http = ClientSession()
        self.ws = await self._http.ws_connect(self.url)
        self._reader = asyncio.create_task(self._read())
        await asyncio.sleep(0.3)         # laisse arriver status + last_data
        return self

    async def __aexit__(self, *exc):
        self._reader.cancel()
        await self.ws.close()
        await self._http.close()

    async def _read(self):
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                break
            m = json.loads(msg.data)
            if m["type"] == "status":
                self.state = m
            elif m["type"] == "data":
                self.last = m
            await self._queue.put(m)

    async def wait_for(self, pred, timeout=RESPONSE_TIMEOUT_S):
        """Attend le premier message qui satisfait pred (ou None après timeout)."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    m = await self._queue.get()
                    if pred(m):
                        return m
        except TimeoutError:
            return None

    async def send(self, msg: dict) -> dict:
        await self.ws.send_json(msg)
        reply = await self.wait_for(
            lambda m: m["type"] in ("ack", "error") and m.get("command") == msg["type"])
        if reply is None or reply["type"] == "error":
            raise RuntimeError(f"{msg['type']} : {reply['error'] if reply else 'pas de réponse du serveur'}")
        return reply

    async def set_resistance(self, level: int, encoding: str | None = None,
                             settle_s: float = 2.5) -> dict:
        """Envoie la commande et renvoie {result, observed} : réponse du rameur
        (indication du Control Point) et niveau lu dans Rower Data après coup.
        L'indication peut arriver avant l'ack du serveur : on écoute tout
        pendant settle_s sans rien jeter."""
        msg = {"type": "set_resistance", "level": level}
        if encoding:
            msg["encoding"] = encoding
        await self.ws.send_json(msg)
        result, observed, acked = "no_indication", None, False
        try:
            async with asyncio.timeout(settle_s):
                while True:
                    m = await self._queue.get()
                    if m.get("command") not in (None, "set_resistance"):
                        continue                      # réponse à une autre commande (ex. program)
                    if m["type"] == "error":
                        raise RuntimeError(f"set_resistance : {m['error']}")
                    if m["type"] == "ack":
                        acked = True
                    elif m["type"] == "control_response":
                        result = m.get("result", m.get("raw"))
                    elif m["type"] == "data":
                        observed = m.get("resistance")
        except TimeoutError:
            pass
        if not acked:
            raise RuntimeError("set_resistance : pas de réponse du serveur")
        return {"result": result, "observed": observed}

    async def program_state(self, **kw):
        await self.ws.send_json({"type": "program", **kw})

    async def check_server(self):
        """Vérifie que le serveur connaît la commande `program` (version à jour)."""
        await self.ws.send_json({"type": "program", "active": False})
        reply = await self.wait_for(lambda m: m["type"] in ("ack", "error") and m.get("command") == "program")
        if reply is None or reply["type"] == "error":
            sys.exit("Le serveur ne connaît pas la commande `program` : relance `python3 server.py`.")


# ── Commandes CLI ─────────────────────────────────────────────────────────────
async def cmd_probe(a):
    """Valide l'encodage par défaut sur 1 / 8 / 15 (un niveau différent par envoi :
    si le rameur ignore une commande, le niveau relu reste sur le précédent).
    --all teste aussi les encodages alternatifs."""
    encodings = ("spec", "uint8", "int16") if a.all else (ftms.RESISTANCE_ENCODING,)
    async with Trainer(a.server) as t:
        if not t.state.get("connected"):
            sys.exit("Le rameur n'est pas connecté au serveur (allume-le et réessaie).")
        initial = t.last.get("resistance")
        print(f"Résistance actuelle lue : {initial}\n")
        verdict = {}
        for enc in encodings:
            ok_all = True
            for level in (8, 1, 15):
                payload = ftms.set_resistance_payload(level, enc).hex()
                try:
                    r = await t.set_resistance(level, enc)
                    ok = r["observed"] == level and r["result"] in ("success", "no_indication")
                except RuntimeError as e:
                    r, ok = {"result": str(e), "observed": None}, False
                ok_all &= ok
                print(f"  {'✅' if ok else '❌'} {enc:6s} niveau {level:2d} payload={payload:8s} "
                      f"réponse={r['result']:22s} résistance lue={r['observed']}")
                await asyncio.sleep(1)
            verdict[enc] = ok_all
        good = [e for e, ok in verdict.items() if ok]
        print(f"\nEncodage(s) valide(s) sur 1/8/15 : {', '.join(good) or 'aucun'}"
              f"  (ftms.RESISTANCE_ENCODING = \"{ftms.RESISTANCE_ENCODING}\")")
        if initial and good:
            r = await t.set_resistance(initial, good[0])
            print(f"Résistance remise à {initial} (lu {r['observed']}).")


async def cmd_set(a):
    async with Trainer(a.server) as t:
        r = await t.set_resistance(a.level, a.encoding)
        print(f"réponse={r['result']} résistance lue={r['observed']}")


async def cmd_stop(a):
    async with Trainer(a.server) as t:
        await t.program_state(active=False)
        if a.lo:
            await t.set_resistance(a.lo, a.encoding)
        print("Programme effacé.")


async def cmd_run(a):
    name, steps = load_program(a.program, a)
    total = sum(s["duration_s"] for s in steps)
    print(f"Programme « {name} » — {len(steps)} étapes, {total // 60} min {total % 60:02d} s")
    for i, s in enumerate(steps, 1):
        print(f"  {i:2d}. {s['label']:<18s} {s['duration_s']:4d} s  R{s['resistance']}")

    async with Trainer(a.server) as t:
        await t.check_server()
        if not t.state.get("connected"):
            print("\nEn attente du rameur (allume-le, la connexion est automatique)…")
            await t.wait_for(lambda m: m["type"] == "status" and m.get("connected"), timeout=None)

        async def publish(idx, remaining, active=True):
            await t.program_state(
                active=active, name=name, step=idx + 1, steps=len(steps),
                label=steps[idx]["label"], resistance=steps[idx]["resistance"],
                remaining_s=max(0, round(remaining)), total_s=total,
                elapsed_s=sum(s["duration_s"] for s in steps[:idx]) + steps[idx]["duration_s"] - remaining)

        idx = 0
        await publish(0, steps[0]["duration_s"])
        if t.state.get("machine_state") != "running":
            print("\nEn attente du premier coup de rame…")
            await t.wait_for(lambda m: m["type"] == "status" and m["machine_state"] == "running",
                             timeout=None)

        base = t.last.get("elapsed_s") or 0            # chrono rameur au départ du programme
        step_start = base
        applied = None
        try:
            while idx < len(steps):
                step = steps[idx]
                if applied != step["resistance"]:
                    applied = step["resistance"]
                    if a.no_send:
                        print(f"→ {step['label']} : consigne R{step['resistance']} "
                              f"(molette actuelle : {t.last.get('resistance')})")
                    else:
                        r = await t.set_resistance(step["resistance"], a.encoding)
                        print(f"→ {step['label']} : consigne R{step['resistance']} "
                              f"(molette : {r['observed']}, rameur : {r['result']})")
                d = await t.wait_for(lambda m: m["type"] == "data", timeout=None)
                elapsed = d.get("elapsed_s") or 0
                if elapsed < step_start:                 # reset du rameur → on repart de l'étape courante
                    base = step_start = elapsed
                remaining = step["duration_s"] - (elapsed - step_start)
                await publish(idx, remaining)
                if remaining <= 0:
                    idx += 1
                    step_start = elapsed
            print("\n✅ Programme terminé.")
            await t.program_state(active=False)
            if a.lo and not a.no_send:
                await t.set_resistance(a.lo, a.encoding)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await t.program_state(active=False)
            print("\nProgramme interrompu.")
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=SERVER)
    p.add_argument("--encoding", choices=("spec", "uint8", "int16"), default=None,
                   help="encodage du niveau (défaut : ftms.RESISTANCE_ENCODING)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("probe", help="valide l'encodage de résistance sur les niveaux 1 / 8 / 15")
    s.add_argument("--all", action="store_true", help="teste aussi uint8 et int16")
    s = sub.add_parser("set", help="règle la résistance");                    s.add_argument("level", type=int)
    s = sub.add_parser("stop", help="efface le programme affiché");           s.add_argument("--lo", type=int, default=None)
    s = sub.add_parser("run", help="lance un programme")
    s.add_argument("program", help="intervals | pyramid | steady | fichier.json")
    s.add_argument("--no-send", action="store_true",
                   help="n'envoie pas la commande BLE, affiche seulement la consigne")
    s.add_argument("--warmup", type=int, default=120); s.add_argument("--cooldown", type=int, default=120)
    s.add_argument("--work", type=int, default=60);    s.add_argument("--rest", type=int, default=60)
    s.add_argument("--reps", type=int, default=6)
    s.add_argument("--hi", type=int, default=8);       s.add_argument("--lo", type=int, default=3)
    s.add_argument("--step", type=int, default=60,     help="pyramid : durée de chaque palier")
    s.add_argument("--level", type=int, default=5);    s.add_argument("--minutes", type=int, default=20)

    a = p.parse_args()
    fn = {"probe": cmd_probe, "set": cmd_set, "run": cmd_run, "stop": cmd_stop}[a.cmd]
    try:
        asyncio.run(fn(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
