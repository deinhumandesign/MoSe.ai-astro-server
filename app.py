from flask import Flask, request, jsonify
import json
import swisseph as swe
import pytz
from pytz.exceptions import AmbiguousTimeError, NonExistentTimeError
from datetime import datetime, timedelta

# ------------------------------------------------------------------------------
# App + Ephemeriden
# ------------------------------------------------------------------------------
app = Flask(__name__)

# Deine Swiss Ephemeris Dateien liegen im Repo unter /ephe (z.B. seas_18.se1)
try:
    swe.set_ephe_path("ephe")
except Exception:
    pass

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "service": "MoSe.ai_astro_server",
        "marker": "v-hd-model-pcts-01",
        "swisseph": getattr(swe, "__version__", "unknown"),
        "status": "live"
    }), 200

# ------------------------------------------------------------------------------
# Basics
# ------------------------------------------------------------------------------
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
ALLOWED_HOUSES = {"P","K","E","W","R","C","B","H","M","T","O"}

def normalize_deg(x: float) -> float:
    x = float(x) % 360.0
    return x if x >= 0 else x + 360.0

def sign_from_lon(lon: float) -> str:
    return SIGNS[int(normalize_deg(lon) // 30) % 12]

def validate_lat_lon(lat, lon):
    lat = float(lat); lon = float(lon)
    if not (-90.0 <= lat <= 90.0):  raise ValueError("latitude außerhalb (-90..90)")
    if not (-180.0 <= lon <= 180.0): raise ValueError("longitude außerhalb (-180..180)")
    return lat, lon

def pick_housesys(val):
    if not val: return b"P"
    code = str(val).strip().upper()
    if code not in ALLOWED_HOUSES:
        raise ValueError(f"houses_system '{code}' wird nicht unterstützt")
    return code.encode("ascii")

def cusps_to_12(cusps):
    if not isinstance(cusps, (list, tuple)): raise ValueError("houses_ex lieferte keine Cusp-Liste")
    n = len(cusps)
    if n >= 13: return [round(float(cusps[i]), 3) for i in range(1, 13)]
    if n == 12: return [round(float(cusps[i]), 3) for i in range(0, 12)]
    raise ValueError(f"houses_ex cusps-Länge unerwartet: {n}")

def extract_lon_lat_speed(res):
    """SwissEph calc_ut liefert entweder ([lon,lat,dist],[spd...]) oder [lon,lat,dist,spd...]"""
    if not isinstance(res, (list, tuple)) or len(res) == 0:
        raise ValueError("calc_ut Ergebnis leer/ungültig")
    first = res[0]
    if isinstance(first, (list, tuple)):  # tuple of tuples
        lon = float(first[0]); lat = float(first[1]) if len(first) > 1 else 0.0
        spd = float(res[1][0]) if len(res) > 1 and isinstance(res[1], (list, tuple)) and len(res[1]) > 0 else 0.0
    else:
        lon = float(res[0]); lat = float(res[1]) if len(res) > 1 else 0.0
        spd = float(res[3]) if len(res) > 3 else 0.0
    return lon, lat, spd

def read_input():
    # JSON Body
    try:
        data = request.get_json(force=True, silent=True)
        if isinstance(data, dict): return data
    except Exception:
        pass
    # Raw JSON
    try:
        raw = request.get_data(as_text=True)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict): return data
    except Exception:
        pass
    # Form
    if request.form:
        return {k: request.form.get(k) for k in request.form.keys()}
    # Query
    if request.args:
        return {k: request.args.get(k) for k in request.args.keys()}
    raise ValueError("kein lesbarer Body/Parameter")

# ------------------------------------------------------------------------------
# Zeit-Parsing
# ------------------------------------------------------------------------------
def parse_ts_from_inputs(data: dict) -> (datetime, dict):
    ts = data.get("timestamp_utc")
    if ts is not None:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(float(ts)), {"mode": "utc_unix"}
        if isinstance(ts, str):
            s = ts.strip().replace(" ", "T")
            if s.replace('.', '', 1).isdigit():
                return datetime.utcfromtimestamp(float(s)), {"mode": "utc_unix_str"}
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt, {"mode": "utc_iso_naive"}
            return dt.astimezone(pytz.UTC).replace(tzinfo=None), {"mode": "utc_iso_tz"}

    date_local = data.get("date_local") or data.get("geburtsdatum")
    time_local = data.get("time_local") or data.get("geburtszeit")
    if not date_local or not time_local:
        raise ValueError("timestamp_utc oder (date_local & time_local) erforderlich")

    time_local = str(time_local).replace(" Uhr", "").strip()
    try:
        dt_local_naive = datetime.strptime(f"{date_local.strip()} {time_local}", "%d.%m.%Y %H:%M")
    except Exception:
        raise ValueError("date_local/time_local Format erwartet: 'D.M.YYYY' und 'H:mm'")

    tz_name = data.get("tz_name")
    if tz_name:
        tz = pytz.timezone(str(tz_name).strip())
        try:
            local_dt = tz.localize(dt_local_naive, is_dst=None)
        except AmbiguousTimeError:
            local_dt = tz.localize(dt_local_naive, is_dst=True)
        except NonExistentTimeError:
            local_dt = tz.localize(dt_local_naive + timedelta(hours=1), is_dst=True)
        return local_dt.astimezone(pytz.UTC).replace(tzinfo=None), {"mode": "local_tzname", "tz_name": tz_name}

    raw = data.get("raw_offset"); dst = data.get("dst_offset")
    if raw is None or dst is None:
        raise ValueError("tz_name oder (raw_offset & dst_offset) erforderlich")
    offset_seconds = int(float(raw)) + int(float(dst))
    return dt_local_naive - timedelta(seconds=offset_seconds), {"mode": "local_offsets", "offset": offset_seconds}

# ------------------------------------------------------------------------------
# HD: Gate/Line/Color/Tone/Base
# ------------------------------------------------------------------------------
GATE_SIZE = 360.0 / 64.0          # 5.625°
LINE_SIZE = GATE_SIZE / 6.0       # 0.9375°
COLOR_SIZE = LINE_SIZE / 6.0
TONE_SIZE  = COLOR_SIZE / 6.0
BASE_SIZE  = TONE_SIZE / 5.0

# Reihenfolge der Gates entlang 0..360; Start bei Gate 25 @ 28°15' Fische
GATE_ORDER = [
    25,17,21,51,42,3, 27,24,2,23,8, 20,16,35,45,12,15,
    52,39,53,62,56, 31,33,7,4,29, 59,40,64,47,6,46,
    18,48,57,32,50, 28,44,1,43,14, 34,9,5,26,11,10,
    58,38,54,61,60, 41,19,13,49,30, 55,37,63,22,36
]
START_DEG = normalize_deg(330 + 28.25)  # 358.25°

def wrap_shift(idx: int, shift: int, length: int) -> int:
    """1-indexed wrap shift"""
    return ((idx - 1 + shift) % length) + 1

def hd_from_lon(lon_deg: float, model: str = "std"):
    """
    model:
      - "std": direkte Vorwärts-Segmentierung
      - "alt": versetzte Zählung (liefert z.B. Sun 19.4 -> color 6, tone 5, base 3)
    """
    x = normalize_deg(lon_deg)
    delta = (x - START_DEG) % 360.0
    gate_idx = int(delta // GATE_SIZE)               # 0..63
    inside_gate = delta - gate_idx * GATE_SIZE

    line_idx0 = int(inside_gate // LINE_SIZE)        # 0..5
    inside_line = inside_gate - line_idx0 * LINE_SIZE

    color_idx0 = int(inside_line // COLOR_SIZE)      # 0..5
    inside_color = inside_line - color_idx0 * COLOR_SIZE

    tone_idx0  = int(inside_color // TONE_SIZE)      # 0..5
    inside_tone = inside_color - tone_idx0 * TONE_SIZE

    base_idx0  = int(inside_tone // BASE_SIZE)       # 0..4
    inside_base = inside_tone - base_idx0 * BASE_SIZE

    # 1-indexierte Werte
    gate = GATE_ORDER[gate_idx]
    line = line_idx0 + 1

    color = color_idx0 + 1
    tone  = tone_idx0 + 1
    base  = base_idx0 + 1

    # Prozent innerhalb der jeweiligen Ebene (0..1)
    color_pct = inside_color / COLOR_SIZE
    tone_pct  = inside_tone / TONE_SIZE
    base_pct  = inside_base / BASE_SIZE

    # Alternatives Zählmodell (konstanter Offsetsatz)
    if str(model).lower() in ("alt", "jovian", "shift"):
        color = wrap_shift(color, +1, 6)   # 5 -> 6, 6 -> 1, ...
        tone  = wrap_shift(tone,  -1, 6)   # 6 -> 5, 1 -> 6, ...
        base  = wrap_shift(base,  -1, 5)   # 4 -> 3, 1 -> 5, ...

    return {
        "gate": gate,
        "line": line,
        "color": color,
        "tone":  tone,
        "base":  base,
        "color_pct": round(float(color_pct), 6),
        "tone_pct":  round(float(tone_pct), 6),
        "base_pct":  round(float(base_pct), 6),
        "model": model
    }

# ------------------------------------------------------------------------------
# Astro: Planeten/Häuser
# ------------------------------------------------------------------------------
PLANETS = {
    "sun": swe.SUN, "moon": swe.MOON, "mercury": swe.MERCURY, "venus": swe.VENUS,
    "mars": swe.MARS, "jupiter": swe.JUPITER, "saturn": swe.SATURN,
    "uranus": swe.URANUS, "neptune": swe.NEPTUNE, "pluto": swe.PLUTO,
    "true_node": swe.TRUE_NODE,
    "lilith_mean": swe.MEAN_APOG,     # MEAN Lilith (astro.com)
    "chiron": swe.CHIRON
}

FLAGS_MOSEPH = swe.FLG_MOSEPH | swe.FLG_SPEED
FLAGS_SWIEPH = swe.FLG_SWIEPH | swe.FLG_SPEED

def calc_houses(jd, lat, lon, hs_code):
    cusps, ascmc = swe.houses_ex(jd, lat, lon, hs_code)
    if not isinstance(ascmc, (list, tuple)) or len(ascmc) < 2:
        raise ValueError("houses_ex ascmc-Länge unerwartet")
    asc = normalize_deg(ascmc[0]); mc = normalize_deg(ascmc[1])
    cusps12 = cusps_to_12(cusps)
    houses = {
        "asc": round(asc, 3),
        "mc": round(mc, 3),
        "cusps": cusps12
    }
    for i, val in enumerate(cusps12, start=1):
        houses[f"c{i}"] = val
    return houses, cusps12

def calc_planets(jd, lat, lon, cusps12, hd_model="std"):
    def house_of(lon_deg):
        L = normalize_deg(lon_deg)
        for i in range(12):
            a = cusps12[i]; b = cusps12[(i+1) % 12]
            if (a <= b and a <= L < b) or (a > b and (L >= a or L < b)):
                return i+1
        return 12

    planets = {}
    for name, pid in PLANETS.items():
        try:
            flags = FLAGS_SWIEPH if name in ("chiron",) else FLAGS_MOSEPH
            res = swe.calc_ut(jd, pid, flags)
            lon_v, lat_v, spd_v = extract_lon_lat_speed(res)
            plon = normalize_deg(lon_v)
            planets[name] = {
                "lon": round(plon, 3),
                "lat": round(float(lat_v), 3),
                "speed": round(float(spd_v), 3),
                "sign": sign_from_lon(plon),
                "house": house_of(plon),
                "hd": hd_from_lon(plon, model=hd_model)
            }
        except Exception as ex:
            planets[name] = {"error": str(ex)}

    # Earth = Sun + 180°
    if "sun" in planets and "error" not in planets["sun"]:
        eplon = normalize_deg(planets["sun"]["lon"] + 180.0)
        planets["earth"] = {
            "lon": round(eplon, 3),
            "lat": 0.0, "speed": 0.0,
            "sign": sign_from_lon(eplon),
            "house": house_of(eplon),
            "hd": hd_from_lon(eplon, model=hd_model)
        }

    # South Node = True Node + 180°
    if "true_node" in planets and "error" not in planets["true_node"]:
        snlon = normalize_deg(planets["true_node"]["lon"] + 180.0)
        planets["south_node"] = {
            "lon": round(snlon, 3),
            "lat": 0.0, "speed": 0.0,
            "sign": sign_from_lon(snlon),
            "house": house_of(snlon),
            "hd": hd_from_lon(snlon, model=hd_model)
        }

    return planets

# ------------------------------------------------------------------------------
# API
# ------------------------------------------------------------------------------
@app.route("/astro", methods=["POST", "GET"])
def astro():
    try:
        data = read_input()

        lat = data.get("latitude"); lon = data.get("longitude")
        if lat is None or lon is None:
            raise ValueError("latitude und longitude sind erforderlich")
        lat, lon = validate_lat_lon(lat, lon)

        hs_code = pick_housesys(data.get("houses_system"))

        # HD-Substruktur-Modell: std (Default) oder alt
        hd_model = str(data.get("hd_model", "std")).lower()

        # Birth
        dt_utc, modeinfo = parse_ts_from_inputs(data)
        jd = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                        dt_utc.hour + dt_utc.minute/60 + dt_utc.second/3600)

        houses_birth, cusps_birth = calc_houses(jd, lat, lon, hs_code)
        planets_birth = calc_planets(jd, lat, lon, cusps_birth, hd_model=hd_model)

        # Design (≈ 88° Solarbogen zurück)
        days_back = 88.0 * 365.2422 / 360.0
        dt_design = dt_utc - timedelta(days=days_back)
        jd_d = swe.julday(dt_design.year, dt_design.month, dt_design.day,
                          dt_design.hour + dt_design.minute/60 + dt_design.second/3600)
        houses_design, cusps_design = calc_houses(jd_d, lat, lon, hs_code)
        planets_design = calc_planets(jd_d, lat, lon, cusps_design, hd_model=hd_model)

        out = {
            "datetime_utc": dt_utc.replace(tzinfo=pytz.UTC).isoformat(),
            "input_echo": {
                "mode": modeinfo,
                "date_local": data.get("date_local") or data.get("geburtsdatum"),
                "time_local": data.get("time_local") or data.get("geburtszeit"),
                "tz_name": data.get("tz_name"),
                "raw_offset": data.get("raw_offset"),
                "dst_offset": data.get("dst_offset"),
                "hd_model": hd_model
            },
            "settings": {
                "houses_system": hs_code.decode("ascii"),
                "flags_moseph": int(FLAGS_MOSEPH),
                "flags_swieph": int(FLAGS_SWIEPH)
            },
            "houses": houses_birth,
            "planets": planets_birth,
            "design": {
                "datetime_utc": dt_design.replace(tzinfo=pytz.UTC).isoformat(),
                "houses": houses_design,
                "planets": planets_design
            }
        }
        return jsonify(out), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ------------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
