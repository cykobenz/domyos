"""
Domyos Woodrower — Lecteur FTMS temps réel (affichage terminal)
"""

import asyncio
import sys
from bleak import BleakClient
from ftms import (ROWER_MAC, ROWER_DATA_UUID, FIT_FEATURE_UUID,
                  parse_rower_data, parse_features, fmt_time, fmt_pace)
from logger import SessionLogger


def build_lines(d: dict, raw: str, session_id: int | None) -> list[str]:
    lines = []
    status = f"session #{session_id}" if session_id else "en attente d'activité"
    lines.append(f"🏋️  Domyos Woodrower — {status}")
    lines.append("─" * 40)
    if "elapsed_s" in d:
        lines.append(f"⏱  Temps      : {fmt_time(d['elapsed_s'])}")
    if "stroke_rate_spm" in d:
        lines.append(f"🚣 Cadence    : {d['stroke_rate_spm']:.1f} coups/min")
    if "stroke_count" in d:
        lines.append(f"🔢 Coups tot. : {d['stroke_count']}")
    if "distance_m" in d:
        lines.append(f"📏 Distance   : {d['distance_m']} m")
    if "pace_s_per_500m" in d:
        lines.append(f"⚡ Allure     : {fmt_pace(d['pace_s_per_500m'])}")
    if "power_w" in d:
        val = f"{d['power_w']} W" if d["power_w"] > 0 else "—"
        lines.append(f"💪 Puissance  : {val}")
    if "energy_total_kcal" in d:
        lines.append(f"🔥 Calories   : {d['energy_total_kcal']} kcal")
    if "heart_rate_bpm" in d:
        lines.append(f"❤️  FC         : {d['heart_rate_bpm']} bpm")
    if "resistance" in d:
        lines.append(f"🎚  Résistance : {d['resistance']}")
    lines.append("─" * 40)
    lines.append(f"Raw: {raw}")
    return lines


_last_line_count = 0


def redraw(lines: list[str]) -> None:
    """Remonte le curseur de _last_line_count lignes et réécrit proprement."""
    global _last_line_count
    if _last_line_count:
        sys.stdout.write(f"\033[{_last_line_count}A")
    for line in lines:
        sys.stdout.write(f"\033[2K{line}\n")
    sys.stdout.flush()
    _last_line_count = len(lines)


async def main():
    log = SessionLogger()

    print(f"Connexion à {ROWER_MAC}…")
    async with BleakClient(ROWER_MAC) as client:
        print("✅ Connecté au Domyos Woodrower")

        feat = parse_features(await client.read_gatt_char(FIT_FEATURE_UUID))
        print(f"Features : {', '.join(feat['machine'])}")
        print(f"Cibles   : {', '.join(feat['targets']) or '—'}")
        print("En écoute… (Ctrl+C pour quitter)\n")

        def on_rower_data(sender, data):
            parsed = parse_rower_data(data)
            log.record(parsed)
            redraw(build_lines(parsed, data.hex(), log.session_id))

        await client.start_notify(ROWER_DATA_UUID, on_rower_data)
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await client.stop_notify(ROWER_DATA_UUID)
            log.close()
            print("Déconnecté.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
