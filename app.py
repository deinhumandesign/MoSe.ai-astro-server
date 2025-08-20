from flask import Flask, request, jsonify
import json
import swisseph as swe
import pytz
from pytz.exceptions import AmbiguousTimeError, NonExistentTimeError
from datetime import datetime, timedelta

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "service": "MoSe.ai_astro_server",
        "marker": "v-tzname-and-mean-lilith-01",
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
    "lilith_true": swe.OSCU_APOG,   # osculating (True) Lilith
    "lilith_mean": swe.MEAN_APOG,   # Mean Lilith
    "chiron": swe.CHIRON            # braucht SE-Datafiles, sonst Fehlerobjekt
}

EPH_FLAGS = swe.FLG_MOSEPH | swe.FLG_SPEED   # ohne SE-Datafiles nutzbar
ALLOWED_HOUSES = {"P","K","E","W","R","C","B","H","M","T","O"}

def normalize_deg(x: float) -> float:
    x = float(x) % 360.0
    return x if x >= 0 else x + 360.0

def sign_from_lon(lon: float) -> str:
    return SIGNS[int(normalize_deg(lon) // 30) % 12]

def parse_ts_from_inputs(data: dict) -> (datetime, dict):
    # 1) Direkter UTC-Timestamp (ISO/Unix)
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

    # 2) Lokales Datum/Zeit + Zeitzone
    date_local = data.get("date_local") or data.get("geburtsdatum")
    time_local = data.get("time_local") or data.get("geburtszeit")
    if not date_local or not time_local:
        raise ValueError("timestamp_utc oder (date_local & time_local) erforderlich")

    time_local = str(time_local).replace(" Uhr", "").strip()
    try:
        dt_local_naive = datetime.strptime(f"{date_local.strip()} {time_local}", "%d.%m.%Y %H:%M")
    except Exception:
        raise ValueError("date_local/time_local Format erwartet: 'D.M.YYYY' und 'H:mm'")

    # a) bevorzugt tz_name
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

    # b) Fallback: raw_offset + dst_offset (Sekunden)
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
    if not isinstance(res, (list, tuple)) or len(res) == 0:
        raise ValueError("calc_ut Ergebnis leer/ungültig")
    first = res[0]
    if isinstance(first, (list, tuple)):  # ([lon,lat,dist], [spd_lon,spd_lat,spd_dist])
        lon = float(first[0]); lat = float(first[1]) if len(first) > 1 else 0.0
        spd = float(res[1][0]) if len(res) > 1 and isinstance(res[1], (list, tuple)) and len(res[1]) > 0 else 0.0
    else:  # [lon,lat,dist,spd_lon,...]
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
    if request.form:
        return {k: request.form.get(k) for k in request.form.keys()}
    if request.args:
        return {k: request.args.get(k) for k in request.args.keys()}
    raise ValueError("kein lesbarer Body/Parameter")

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
        try:
            cusps, ascmc = swe.houses_ex(jd, lat, lon, hs_code)
        except Exception:
            cusps, ascmc = swe.houses_ex(jd, lat, lon, b"W")
            warnings.append("houses_system_fallback_to_W"); hs_code = b"W"

        if not isinstance(ascmc, (list, tuple)) or len(ascmc) < 2:
            raise ValueError("houses_ex ascmc-Länge unerwartet")

        asc = normalize_deg(ascmc[0]); mc = normalize_deg(ascmc[1])
        cusps12 = cusps_to_12(cusps)

        planets = {}
        for name, pid in PLANETS.items():
            try:
                res = swe.calc_ut(jd, pid, EPH_FLAGS)
                lon_v, lat_v, spd_v = extract_lon_lat_speed(res)
                plon = normalize_deg(lon_v)
                planets[name] = {
                    "lon": round(plon, 3),
                    "lat": round(float(lat_v), 3),
                    "speed": round(float(spd_v), 3),
                    "sign": sign_from_lon(plon)
                }
            except Exception as ex:
                planets[name] = {"error": str(ex)}; warnings.append(f"{name}_calc_failed")

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
            "settings": {"houses_system": hs_code.decode("ascii"), "flags": int(EPH_FLAGS)},
            "planets": planets,
            "houses": {"asc": round(asc, 3), "mc": round(mc, 3), "cusps": cusps12}
        }
        if warnings: out["warnings"] = warnings
        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
