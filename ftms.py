"""
Domyos Woodrower — Protocole FTMS (Fitness Machine Service, UUID 0x1826)

Module sans dépendance externe : constantes GATT, parsing de la
characteristic Rower Data (0x2AD1), décodage de Fitness Machine Feature
(0x2ACC) et opcodes du Control Point (0x2AD9).

Importé par read_rower.py, server.py et trainer.py.
"""

import struct

# ── Identité du rameur ────────────────────────────────────────────────────────
ROWER_MAC  = "1C:2A:85:46:23:5A"
ROWER_NAME = "Domyos-ROW-0743"

# ── UUIDs GATT ────────────────────────────────────────────────────────────────
FTMS_SERVICE_UUID   = "00001826-0000-1000-8000-00805f9b34fb"
ROWER_DATA_UUID     = "00002ad1-0000-1000-8000-00805f9b34fb"   # Notify
FIT_FEATURE_UUID    = "00002acc-0000-1000-8000-00805f9b34fb"   # Read
CONTROL_POINT_UUID  = "00002ad9-0000-1000-8000-00805f9b34fb"   # Write / Indicate
FIT_STATUS_UUID     = "00002ada-0000-1000-8000-00805f9b34fb"   # Notify
TRAINING_STATUS_UUID = "00002ad3-0000-1000-8000-00805f9b34fb"  # Notify

# ── Control Point (0x2AD9) — opcodes ──────────────────────────────────────────
OP_REQUEST_CONTROL        = 0x00
OP_RESET                  = 0x01
OP_SET_TARGET_RESISTANCE  = 0x04
OP_START_RESUME           = 0x07
OP_STOP_PAUSE             = 0x08
OP_RESPONSE_CODE          = 0x80

STOP_PARAM_STOP  = 0x01
STOP_PARAM_PAUSE = 0x02

# Codes de résultat renvoyés dans l'indication 0x80
CP_RESULT = {
    0x01: "success",
    0x02: "op_code_not_supported",
    0x03: "invalid_parameter",
    0x04: "operation_failed",
    0x05: "control_not_permitted",
}

# ── Rower Data (0x2AD1) — flags ───────────────────────────────────────────────
# bit 0 est inversé : 0 = stroke rate + stroke count PRÉSENTS
FLAG_MORE_DATA       = 0x0001
FLAG_AVG_STROKE_RATE = 0x0002
FLAG_TOTAL_DISTANCE  = 0x0004
FLAG_INST_PACE       = 0x0008
FLAG_AVG_PACE        = 0x0010
FLAG_INST_POWER      = 0x0020
FLAG_AVG_POWER       = 0x0040
FLAG_RESISTANCE      = 0x0080
FLAG_EXPENDED_ENERGY = 0x0100
FLAG_HEART_RATE      = 0x0200
FLAG_MET             = 0x0400
FLAG_ELAPSED_TIME    = 0x0800
FLAG_REMAINING_TIME  = 0x1000


def parse_rower_data(data: bytes) -> dict:
    """
    Décode un paquet Rower Data (0x2AD1) selon la spec FTMS.

    Retourne un dict dont les clés ne sont présentes que si le champ
    correspondant est dans le paquet :
        flags_raw, stroke_rate_spm, stroke_count, avg_stroke_rate_spm,
        distance_m, pace_s_per_500m, avg_pace_s_per_500m, power_w,
        avg_power_w, resistance, energy_total_kcal, energy_per_hour_kcal,
        energy_per_min_kcal, heart_rate_bpm, met, elapsed_s, remaining_s
    """
    data = bytes(data)
    flags = struct.unpack_from("<H", data, 0)[0]
    offset = 2
    result = {"flags_raw": f"0x{flags:04X}"}

    if not (flags & FLAG_MORE_DATA):             # stroke rate (0.5/min) + count
        result["stroke_rate_spm"] = data[offset] * 0.5
        result["stroke_count"] = struct.unpack_from("<H", data, offset + 1)[0]
        offset += 3

    if flags & FLAG_AVG_STROKE_RATE:
        result["avg_stroke_rate_spm"] = data[offset] * 0.5
        offset += 1

    if flags & FLAG_TOTAL_DISTANCE:              # UINT24, mètres
        result["distance_m"] = int.from_bytes(data[offset:offset + 3], "little")
        offset += 3

    if flags & FLAG_INST_PACE:                   # UINT16, s / 500 m
        result["pace_s_per_500m"] = struct.unpack_from("<H", data, offset)[0]
        offset += 2

    if flags & FLAG_AVG_PACE:
        result["avg_pace_s_per_500m"] = struct.unpack_from("<H", data, offset)[0]
        offset += 2

    if flags & FLAG_INST_POWER:                  # SINT16, W
        result["power_w"] = struct.unpack_from("<h", data, offset)[0]
        offset += 2

    if flags & FLAG_AVG_POWER:
        result["avg_power_w"] = struct.unpack_from("<h", data, offset)[0]
        offset += 2

    if flags & FLAG_RESISTANCE:                  # SINT16
        result["resistance"] = struct.unpack_from("<h", data, offset)[0]
        offset += 2

    if flags & FLAG_EXPENDED_ENERGY:             # UINT16 + UINT16 + UINT8
        total, per_hour = struct.unpack_from("<HH", data, offset)
        result["energy_total_kcal"] = total
        result["energy_per_hour_kcal"] = per_hour
        result["energy_per_min_kcal"] = data[offset + 4]
        offset += 5

    if flags & FLAG_HEART_RATE:                  # UINT8, bpm
        result["heart_rate_bpm"] = data[offset]
        offset += 1

    if flags & FLAG_MET:                         # UINT8, résolution 0.1
        result["met"] = data[offset] * 0.1
        offset += 1

    if flags & FLAG_ELAPSED_TIME:                # UINT16, s
        result["elapsed_s"] = struct.unpack_from("<H", data, offset)[0]
        offset += 2

    if flags & FLAG_REMAINING_TIME:
        result["remaining_s"] = struct.unpack_from("<H", data, offset)[0]
        offset += 2

    return result


# ── Fitness Machine Feature (0x2ACC) ──────────────────────────────────────────
_MACHINE_FEATURES = [
    "average_speed", "cadence", "total_distance", "inclination",
    "elevation_gain", "pace", "step_count", "resistance_level",
    "stride_count", "expended_energy", "heart_rate", "metabolic_equivalent",
    "elapsed_time", "remaining_time", "power_measurement",
    "force_on_belt_and_power_output", "user_data_retention",
]
_TARGET_FEATURES = [
    "speed_target", "inclination_target", "resistance_target", "power_target",
    "heart_rate_target", "expended_energy_target", "step_number_target",
    "stride_number_target", "distance_target", "training_time_target",
    "time_in_two_hr_zones", "time_in_three_hr_zones", "time_in_five_hr_zones",
    "indoor_bike_simulation", "wheel_circumference", "spin_down_control",
    "cadence_target",
]


def parse_features(data: bytes) -> dict:
    """
    Décode les 8 octets de Fitness Machine Feature en deux listes :
        {"machine": [...], "targets": [...]}
    Pour le Woodrower (a656000004000000) : cadence, distance, pace,
    resistance_level, expended_energy, heart_rate, elapsed_time,
    power_measurement ; cible supportée : resistance_target.
    """
    machine, targets = struct.unpack_from("<II", bytes(data), 0)
    return {
        "machine": [n for i, n in enumerate(_MACHINE_FEATURES) if machine & (1 << i)],
        "targets": [n for i, n in enumerate(_TARGET_FEATURES) if targets & (1 << i)],
    }


# ── Helpers d'affichage ───────────────────────────────────────────────────────
def fmt_time(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def fmt_pace(pace_s: int | None) -> str:
    if not pace_s:
        return "—"
    return f"{pace_s // 60}:{pace_s % 60:02d} /500m"


# ── Fitness Machine Status (0x2ADA) ───────────────────────────────────────────
MACHINE_STATUS = {
    0x01: "reset",
    0x02: "stopped_or_paused",       # param : 0x01 = stop, 0x02 = pause
    0x03: "stopped_safety_key",
    0x04: "started_or_resumed",
    0x05: "target_speed_changed",
    0x06: "target_incline_changed",
    0x07: "target_resistance_changed",
    0x08: "target_power_changed",
    0x09: "target_heart_rate_changed",
    0x0A: "target_energy_changed",
    0x0B: "target_steps_changed",
    0x0C: "target_strides_changed",
    0x0D: "target_distance_changed",
    0x0E: "target_time_changed",
    0xFF: "control_permission_lost",
}


def parse_machine_status(data: bytes) -> dict:
    """Décode une notification Fitness Machine Status → {code, name, param}."""
    data = bytes(data)
    code = data[0]
    name = MACHINE_STATUS.get(code, f"unknown_0x{code:02X}")
    param = None
    if code == 0x02 and len(data) > 1:
        param = {STOP_PARAM_STOP: "stop", STOP_PARAM_PAUSE: "pause"}.get(data[1], data[1])
        name = "stopped" if data[1] == STOP_PARAM_STOP else "paused"
    elif code == 0x07 and len(data) > 1:
        param = data[1]
    return {"code": code, "name": name, "param": param, "raw": data.hex()}


def parse_control_response(data: bytes) -> dict:
    """Décode une indication du Control Point (0x80, opcode, result)."""
    data = bytes(data)
    if len(data) < 3 or data[0] != OP_RESPONSE_CODE:
        return {"raw": data.hex()}
    return {
        "request_opcode": data[1],
        "result": CP_RESULT.get(data[2], f"unknown_0x{data[2]:02X}"),
        "raw": data.hex(),
    }


# Encodage du paramètre de Set Target Resistance Level.
#   "spec"  : UINT8, résolution 0.1 (niveau 5 → 0x32)   ← spec FTMS officielle
#   "uint8" : UINT8 brut (niveau 5 → 0x05)
#   "int16" : SINT16 brut (niveau 5 → 0x05 0x00)
# Validé sur le Woodrower (2026-10-13) : "spec" accepté pour 1 (0x0A), 3 (0x1E) et
# 15 (0x96), indication `success` et niveau relu correct. "uint8" refusé en séance
# (3 et 8 → invalid_parameter). Ne pas changer sans re-tester avec `trainer.py probe`.
RESISTANCE_ENCODING = "spec"


def set_resistance_payload(level: int, encoding: str = RESISTANCE_ENCODING) -> bytes:
    if encoding == "spec":
        return bytes([OP_SET_TARGET_RESISTANCE, level * 10])
    if encoding == "uint8":
        return bytes([OP_SET_TARGET_RESISTANCE, level])
    if encoding == "int16":
        return bytes([OP_SET_TARGET_RESISTANCE]) + struct.pack("<h", level)
    raise ValueError(encoding)
