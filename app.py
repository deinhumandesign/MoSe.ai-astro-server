from flask import Flask, request, jsonify
import json, os
import swisseph as swe
import pytz
from pytz.exceptions import AmbiguousTimeError, NonExistentTimeError
from datetime import datetime, timedelta

app = Flask(__name__)

# ---- Swiss Ephemeris: mehrere Suchpfade (Root + ./ephe + Systempfade) ----
ROOT_DIR = os.path.dirname(__file__)
CANDIDATE_DIRS = [
    os.path.join(ROOT_DIR, "ephe"),
    ROOT_DIR,
    "/usr/share/swisseph",
    "/usr/local/share/swisseph",
]
# nur existierende Pfade übernehmen
swe.set_ephe_path(":".join([p for p in CANDIDATE_DIRS if os.path.isdir(p)]))

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "service": "MoSe.ai_astro_server",
        "marker": "v-earth-and-planet-houses-01",
        "swisseph": getattr(swe, "__version__", "unknown"),
        "status": "live"
    }), 200

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

PLANETS = {
    "sun": swe.SUN, "moon": swe.MOON, "mercury": swe.MERCURY, "venus": swe.VENUS,
    "mars": swe.MARS, "jupiter": swe.JUPITER, "saturn": swe.SATURN,
    "uranus": swe.URANUS, "neptune": swe.NEPTUNE, "pluto": swe.PLUTO,
    "true_node": swe.TRUE_NODE,
    "lilith_true": swe.OSCU_APOG,   # True/Osculating Lilith
    "lilith_mean": swe.MEAN_APOG,   # Mean Lilith (astro.com Standard)
    "chiron": swe.CHIRON            # benötigt seas_18.se1 (liegt bei dir in /ephe)
}

EPH_FLAGS_MOSEPH = swe.FLG_MOSEPH | swe.FLG_SPEED   # ohne SE-Files
EPH_FLAGS_SWIEPH = swe.FLG_SWIEPH | swe.FLG_SPEED   # mit SE-Files (für Chiron)
ALLOWED_HOUSES = {"P","K","E","W","R","C","B","H","M","T","O"}

def normalize_deg(x: float) -> float:
    x = float(x) % 360.0
    return x if x >= 0 else x + 360.0

def sign_from_lon(lon: float) -> str:
    return SIGNS[int(normalize_deg(lon) // 30) % 12]

def parse_ts_from_inputs(data: dict):
    ts = data.get("timestamp_utc")
    if ts is not None:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(float(ts)), {"mode": "utc_unix"}
        if isinstance(ts, str):
            s = ts.strip().replace(" ", "T")
            if s.replace('.', '', 1).isdigit():
                return datetime.utcfromtimestamp(float(s)), {"mode": "utc_unix_str"}
            if s.endswith("Z"): s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None: return dt, {"mode": "utc_iso_naive"}
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
    # ([lon,lat,dist],[spd_lon,...]) oder [lon,lat,dist,spd_lon,...]
    if not isinstance(res, (list, tuple)) or len(res) == 0:
        raise ValueError("calc_ut Ergebnis leer/ungültig")
    first = res[0]
    if isinstance(first, (list, tuple)):
        lon = float(first[0]); lat = float(first[1]) if len(first) > 1 else 0.0
        spd = float(res[1][0]) if len(res) > 1 and isinstance(res[1], (list, tuple)) and len(res[1]) > 0 else 0.0
    else:
        lon = float(res[0]); lat = float(res[1]) if len(res) > 1 else 0.0
        spd = float(res[3]) if len(res) > 3 else 0.0
    return lon, lat, spd

def read_input():
    try:
        data = request.get_json(force=True, silent=True)
        if isinstance(data, dict): return data
    except Exception: pass
    try:
        raw = request.get_data(as_text=True)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict): return data
    except Exception: pass
    if request.form: return {k: request.form.get(k) for k in request.form.keys()}
    if request.args: return {k: request.args.get(k) for k in request.args.keys()}
    raise ValueError("kein lesbarer Body/Parameter")

# --- Haus-Bestimmung ---
def house_of(longitude: float, cusps12):
    """
    Gibt Hausnummer (1..12) für eine ekliptikale Länge zurück.
    Wir gehen die Bögen von Cusp_i -> Cusp_{i+1} im Uhrzeigersinn (0..360) durch.
    """
    # Kanten vorbereiten (c13 = c1 + 360)
    c = [float(x) for x in cusps12]
    c13 = c[0] + 360.0
    edges = c + [c13]

    L = normalize_deg(float(longitude))
    for i in range(12):
        start = edges[i]
        end = edges[i+1]
        # Planet auf den Bereich [start, start+360) heben
        P = L if L >= start else L + 360.0
        if start <= P < end:
            return i + 1
    return 12  # Fallback

@app.route("/astro", methods=["POST", "GET"])
def astro():
    try:
        data = read_input()

        lat = data.get("latitude"); lon = data.get("longitude")
        if lat is None or lon is None:
            raise ValueError("latitude und longitude sind erforderlich")
        lat, lon = validate_lat_lon(lat, lon)

        hs_code = pick_housesys(data.get("houses_system"))

        dt_utc, modeinfo = parse_ts_from_inputs(data)
        jd = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                        dt_utc.hour + dt_utc.minute/60 + dt_utc.second/3600)

        warnings = []
        # Häuser
        try:
            cusps, ascmc = swe.houses_ex(jd, lat, lon, hs_code)
        except Exception:
            cusps, ascmc = swe.houses_ex(jd, lat, lon, b"W")
            warnings.append("houses_system_fallback_to_W"); hs_code = b"W"

        if not isinstance(ascmc, (list, tuple)) or len(ascmc) < 2:
            raise ValueError("houses_ex ascmc-Länge unerwartet")

        asc = normalize_deg(ascmc[0]); mc = normalize_deg(ascmc[1])
        cusps12 = cusps_to_12(cusps)

        # Planeten & Punkte
        planets = {}
        for name, pid in PLANETS.items():
            try:
                flags = EPH_FLAGS_SWIEPH if name == "chiron" else EPH_FLAGS_MOSEPH
                res = swe.calc_ut(jd, pid, flags)
                lon_v, lat_v, spd_v = extract_lon_lat_speed(res)
                plon = normalize_deg(lon_v)
                p = {
                    "lon": round(plon, 3),
                    "lat": round(float(lat_v), 3),
                    "speed": round(float(spd_v), 3),
                    "sign": sign_from_lon(plon)
                }
                # Hauszuordnung
                p["house"] = house_of(plon, cusps12)
                planets[name] = p
            except Exception as ex:
                planets[name] = {"error": str(ex)}; warnings.append(f"{name}_calc_failed")

        # Südlicher Mondknoten (aus True Node) – ebenfalls Haus berechnen
        if "true_node" in planets and "lon" in planets["true_node"]:
            sn_lon = normalize_deg(planets["true_node"]["lon"] + 180.0)
            p = {
                "lon": round(sn_lon, 3),
                "lat": planets["true_node"].get("lat", 0.0),
                "speed": planets["true_node"].get("speed", 0.0),
                "sign": sign_from_lon(sn_lon),
                "house": house_of(sn_lon, cusps12)
            }
            planets["south_node"] = p

        # EARTH = Sonne + 180°
        if "sun" in planets and "lon" in planets["sun"]:
            e_lon = normalize_deg(planets["sun"]["lon"] + 180.0)
            planets["earth"] = {
                "lon": round(e_lon, 3),
                "lat": 0.0,
                "speed": planets["sun"].get("speed", 0.0),
                "sign": sign_from_lon(e_lon),
                "house": house_of(e_lon, cusps12)
            }

        houses_out = {
            "asc": round(asc, 3),
            "mc": round(mc, 3),
            "cusps": cusps12,
            # feste Keys zum einfachen Mappen
            "c1": cusps12[0], "c2": cusps12[1], "c3": cusps12[2], "c4": cusps12[3],
            "c5": cusps12[4], "c6": cusps12[5], "c7": cusps12[6], "c8": cusps12[7],
            "c9": cusps12[8], "c10": cusps12[9], "c11": cusps12[10], "c12": cusps12[11],
        }

        out = {
            "datetime_utc": dt_utc.replace(tzinfo=pytz.UTC).isoformat(),
            "input_echo": {
                "mode": modeinfo,
                "date_local": data.get("date_local") or data.get("geburtsdatum"),
                "time_local": data.get("time_local") or data.get("geburtszeit"),
                "tz_name": data.get("tz_name"),
                "raw_offset": data.get("raw_offset"),
                "dst_offset": data.get("dst_offset")
            },
            "settings": {
                "houses_system": hs_code.decode("ascii"),
                "flags_moseph": int(EPH_FLAGS_MOSEPH),
                "flags_swieph": int(EPH_FLAGS_SWIEPH)
            },
            "planets": planets,
            "houses": houses_out
        }
        if warnings: out["warnings"] = warnings
        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    # lokal ok – auf Render startet gunicorn via $PORT
    app.run(host="0.0.0.0", port=8080)
