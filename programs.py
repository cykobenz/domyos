"""
Domyos Woodrower — Catalogue et construction des programmes d'entraînement

Un programme est une liste d'étapes {"label", "duration_s", "resistance"} ;
la résistance est la position de molette à afficher en consigne (le Woodrower
n'est pas motorisé) ; None = molette libre (pas de consigne, pas d'alerte). Les presets vivent dans `programs.json` (modifiable sans
toucher au code) ; `build()` les transforme en étapes.

    from programs import presets, build, build_from_json
    name, steps = build(presets()[0])
    name, steps = build({"type": "intervals", "params": {...}})
"""

import json
from pathlib import Path

PRESETS_PATH = Path(__file__).parent / "programs.json"

DEFAULTS = {
    "intervals": {"warmup": 120, "work": 60, "rest": 60, "reps": 6, "hi": 8, "lo": 3, "cooldown": 120},
    "pyramid":   {"lo": 2, "hi": 8, "step": 60},
    "steady":    {"level": 5, "minutes": 20},
}


def presets() -> list[dict]:
    """Presets de programs.json, chacun enrichi de name/duration_s."""
    data = json.loads(PRESETS_PATH.read_text()) if PRESETS_PATH.exists() else {}
    out = []
    for p in data.get("presets", []):
        name, steps = build(p)
        out.append({**p, "name": name, "duration_s": sum(s["duration_s"] for s in steps),
                    "steps_count": len(steps)})
    return out


def preset(pid: str) -> dict | None:
    return next((p for p in presets() if p["id"] == pid), None)


def build(spec: dict) -> tuple[str, list[dict]]:
    """spec = {"type": intervals|pyramid|steady|steps, "params": {...}} (+ "name", "steps")."""
    kind = spec.get("type", "steps")
    params = {**DEFAULTS.get(kind, {}), **(spec.get("params") or {})}
    if kind == "intervals":
        steps = [{"label": "Échauffement", "duration_s": params["warmup"], "resistance": params["lo"]}]
        for i in range(1, params["reps"] + 1):
            steps.append({"label": f"Effort {i}/{params['reps']}", "duration_s": params["work"], "resistance": params["hi"]})
            steps.append({"label": f"Récup {i}/{params['reps']}", "duration_s": params["rest"], "resistance": params["lo"]})
        steps.append({"label": "Retour au calme", "duration_s": params["cooldown"], "resistance": params["lo"]})
        default_name = "Intervalles"
    elif kind == "pyramid":
        levels = list(range(params["lo"], params["hi"] + 1)) + list(range(params["hi"] - 1, params["lo"] - 1, -1))
        steps = [{"label": f"Palier {lv}", "duration_s": params["step"], "resistance": lv} for lv in levels]
        default_name = "Pyramide"
    elif kind == "steady":
        steps = [{"label": f"Palier {params['level']}", "duration_s": params["minutes"] * 60, "resistance": params["level"]}]
        default_name = "Palier"
    elif kind == "steps":
        steps = list(spec.get("steps") or [])
        default_name = "Programme"
    else:
        raise ValueError(f"type de programme inconnu : {kind}")
    # resistance absente / vide / 0 → None : étape « molette libre » (aucune consigne, pas d'alerte)
    steps = [{"label": str(s.get("label", f"Étape {i}")), "duration_s": int(s["duration_s"]),
              "resistance": int(s["resistance"]) if s.get("resistance") not in (None, "", 0, "0") else None}
             for i, s in enumerate(steps, 1)]
    # étapes nulles (ex. warmup 0) retirées
    steps = [s for s in steps if s["duration_s"] > 0]
    if not steps:
        raise ValueError("programme vide")
    return spec.get("name") or default_name, steps


def build_from_json(path: str | Path) -> tuple[str, list[dict]]:
    """Fichier utilisateur : liste d'étapes, ou {"name", "steps"} / {"type", "params"}."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        data = {"type": "steps", "steps": data}
    if "steps" in data and "type" not in data:
        data["type"] = "steps"
    name, steps = build(data)
    return (data.get("name") or Path(path).stem), steps


def _slug(text: str) -> str:
    import re, unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_") or "programme"


def save_preset(body: dict) -> str:
    """Ajoute un preset (ou remplace celui de même id) dans programs.json. Renvoie l'id."""
    kind = body.get("kind") or body.get("type") or "steps"
    spec = {"type": kind, "name": body.get("name"), "params": body.get("params"), "steps": body.get("steps")}
    name, steps = build(spec)                      # valide
    pid = body.get("id") or _slug(name)
    entry = {"id": pid, "name": name, "desc": body.get("desc", ""), "type": kind}
    if kind == "steps":
        entry["steps"] = steps
    else:
        entry["params"] = spec["params"] or {}
    data = json.loads(PRESETS_PATH.read_text()) if PRESETS_PATH.exists() else {"presets": []}
    data["presets"] = [p for p in data.get("presets", []) if p.get("id") != pid] + [entry]
    PRESETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return pid


def delete_preset(pid: str) -> bool:
    if not PRESETS_PATH.exists():
        return False
    data = json.loads(PRESETS_PATH.read_text())
    before = len(data.get("presets", []))
    data["presets"] = [p for p in data.get("presets", []) if p.get("id") != pid]
    if len(data["presets"]) == before:
        return False
    PRESETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True
