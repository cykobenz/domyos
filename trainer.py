"""
Domyos Woodrower — Programmes d'entraînement (client en ligne de commande)

Le programme est déroulé par server.py (ProgramRunner) sur le chrono du rameur :
démarre au premier coup, se fige en pause, repart de l'étape courante après un
reset. Le dashboard affiche la consigne de molette avec alerte et bip. Ce client
sert à lancer / suivre / arrêter un programme depuis un terminal ; le dashboard
peut faire la même chose avec la touche P.

Sur le Woodrower la résistance se règle à la molette : la console accepte la
commande FTMS Set Target Resistance à l'arrêt mais la refuse en séance et ne
bouge jamais la résistance mécanique. Les programmes sont donc des consignes.

    python3 trainer.py list                   # presets de programs.json
    python3 trainer.py run intervalles_courts # un preset par son id
    python3 trainer.py run intervals          # --warmup 120 --work 60 --rest 60 --reps 6 --hi 8 --lo 3 --cooldown 120
    python3 trainer.py run pyramid            # --lo 2 --hi 8 --step 60
    python3 trainer.py run steady             # --level 5 --minutes 20
    python3 trainer.py run mon_programme.json # [{"label": "...", "duration_s": 60, "resistance": 5}, ...]
                                              # --detach : lance et rend la main (le serveur continue)
    python3 trainer.py stop                   # arrête le programme en cours
    python3 trainer.py probe | set N          # tests du Control Point (résistance)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from aiohttp import ClientSession, WSMsgType

import ftms
import programs

SERVER = "ws://localhost:8765/ws"
RESPONSE_TIMEOUT_S = 4.0


# ── Programmes ───────────────────────────────────────────────────────────────
def load_program(name: str, a) -> tuple[str, list[dict]]:
    """Preset (id de programs.json), type intégré paramétré par la CLI, ou fichier JSON."""
    if (ps := programs.preset(name)):
        return programs.build(ps)
    if name == "intervals":
        return programs.build({"type": name, "params": {
            "warmup": a.warmup, "work": a.work, "rest": a.rest, "reps": a.reps,
            "hi": a.hi, "lo": a.lo, "cooldown": a.cooldown}})
    if name == "pyramid":
        return programs.build({"type": name, "params": {"lo": a.lo, "hi": a.hi, "step": a.step}})
    if name == "steady":
        return programs.build({"type": name, "params": {"level": a.level, "minutes": a.minutes}})
    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return programs.build_from_json(path)
    ids = ", ".join(p["id"] for p in programs.presets())
    sys.exit(f"Programme inconnu : {name}\nPresets : {ids}\nTypes : intervals, pyramid, steady, fichier.json")


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

    async def program_start(self, name: str, steps: list[dict]) -> dict:
        try:
            return await self.send({"type": "program_start", "name": name, "kind": "steps",
                                    "steps": steps, "source": "trainer"})
        except RuntimeError as e:
            sys.exit(f"{e}\n(serveur trop ancien ? relance `python3 server.py`)")


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
        await t.send({"type": "program_stop"})
        print("Programme arrêté.")


async def cmd_list(a):
    for p in programs.presets():
        d = p["duration_s"]
        print(f"  {p['id']:<20s} {p['name']:<20s} {d // 60:3d} min {d % 60:02d} s   {p.get('desc', '')}")


async def cmd_run(a):
    name, steps = load_program(a.program, a)
    total = sum(s["duration_s"] for s in steps)
    print(f"Programme « {name} » — {len(steps)} étapes, {total // 60} min {total % 60:02d} s")
    for i, s in enumerate(steps, 1):
        print(f"  {i:2d}. {s['label']:<18s} {s['duration_s']:4d} s  R{s['resistance']}")

    async with Trainer(a.server) as t:
        await t.program_start(name, steps)
        if a.detach:
            print("\nProgramme envoyé au serveur ; il démarre au premier coup de rame.")
            return
        if not t.state.get("connected"):
            print("\nEn attente du rameur (allume-le, la connexion est automatique)…")
        elif t.state.get("machine_state") != "running":
            print("\nEn attente du premier coup de rame…")
        shown = None
        try:
            while True:
                m = await t.wait_for(lambda m: m["type"] == "program", timeout=None)
                if not m.get("active"):
                    print("\n✅ Programme terminé." if m.get("finished") else "\nProgramme arrêté.")
                    return
                key = (m["step"], m.get("started"))
                if key != shown:
                    shown = key
                    if m.get("started"):
                        print(f"→ {m['label']} ({m['step']}/{m['steps']}) : consigne R{m['resistance']}"
                              f" pendant {m['remaining_s']} s"
                              + (f" — ensuite {m['next_label']} R{m['next_resistance']}" if m.get("next_label") else ""))
        except (KeyboardInterrupt, asyncio.CancelledError):
            await t.ws.send_json({"type": "program_stop"})
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
    sub.add_parser("stop", help="arrête le programme en cours")
    sub.add_parser("list", help="liste les presets de programs.json")
    s = sub.add_parser("run", help="lance un programme")
    s.add_argument("program", help="intervals | pyramid | steady | fichier.json")
    s.add_argument("--detach", action="store_true", help="lance le programme et rend la main")
    s.add_argument("--no-send", action="store_true", help=argparse.SUPPRESS)   # obsolète (plus d'envoi de résistance)
    s.add_argument("--warmup", type=int, default=120); s.add_argument("--cooldown", type=int, default=120)
    s.add_argument("--work", type=int, default=60);    s.add_argument("--rest", type=int, default=60)
    s.add_argument("--reps", type=int, default=6)
    s.add_argument("--hi", type=int, default=8);       s.add_argument("--lo", type=int, default=3)
    s.add_argument("--step", type=int, default=60,     help="pyramid : durée de chaque palier")
    s.add_argument("--level", type=int, default=5);    s.add_argument("--minutes", type=int, default=20)

    a = p.parse_args()
    fn = {"probe": cmd_probe, "set": cmd_set, "run": cmd_run, "stop": cmd_stop, "list": cmd_list}[a.cmd]
    try:
        asyncio.run(fn(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
