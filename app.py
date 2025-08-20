from flask import Flask, request, jsonify
import json
import swisseph as swe
import pytz
from datetime import datetime

app = Flask(__name__)

# -------- Health & Version --------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "service": "MoSe.ai_astro_server",
        "marker": "v-cusps-len-fix-01",
        "swisseph": getattr(swe, "__version__", "unknown"),
        "status": "live"
    }), 200


# -------- Konstanten & Konfiguration --------
SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

# True Node & True Lilith (osculating apogee)
PLANETS = {
    "sun": swe.SUN, "moon": swe.MOON, "mercury": swe.MERCURY, "venus": swe.VENUS,
    "mars": swe.MARS, "jupiter": swe.JUPITER, "saturn": swe.SATURN,
    "uranus": swe.URANUS, "neptune": swe.NEPTUNE, "pluto": swe.PLUTO,
    "chiron": swe.CHIRON, "true_node": swe.TRUE_NODE,
    "lilith": swe.OSCU_APOG
}

EPH_FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED
ALLOWED_HOUSES = {"P","K","E","W","R","C","B","H","M","T","O"}


# -------- Helper --------
def normalize_deg(x: float) -> float:
    x = float(x) % 360.0
    return x if x >= 0 else x + 360.0

def sign_from_lon(lon: float) -> str:
    return SIGNS[int(normalize_deg(lon) // 30) % 12]

def parse_ts(ts_val):
    """Akzeptiert Unix (int/float/Ziffern-String) oder ISO (mit/ohne Z/Whitespace)."""
    if ts_val is None:
        raise ValueError("timestamp_utc fehlt")
    if isinstance(ts_val, (int, float)):
        return datetime.utcfromtimestamp(float(ts_val))
    if isinstance(ts_val, str):
        s = ts_val.strip()
        if s.replace('.', '', 1).isdigit():
            return datetime.utcfromtimestamp(float(s))
        s = s.replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(pytz.UTC).replace(tzinfo=None)
    raise ValueError("timestamp_utc hat ein nicht unterstütztes Format")

def validate_lat_lon(lat, lon):
    lat = float(lat); lon = float(lon)
    if not (-90.0 <= lat <= 90.0):
        raise ValueError("latitude außerhalb des gültigen Bereichs (-90..90)")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError("longitude außerhalb des gültigen Bereichs (-180..180)")
    return lat, lon

def pick_housesys(val):
    if not val:
        return b"P"  # Default: Placidus
    code = str(val).strip().upper()
    if code not in ALLOWED_HOUSES:
        raise ValueError(f"houses_system '{code}' wird nicht unterstützt")
    return code.encode("ascii")

def read_input():
    """Nimmt Request-Daten an (JSON, Raw-JSON, Form, Query)."""
    try:
        data = request.get_json(force=True, silent=True)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        raw = request.get_data(as_text=True)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    if request.form:
        return {k: request.form.get(k) for k in request.form.keys()}
    if request.args:
        return {k: request.args.get(k) for k in request.args.keys()}
    raise ValueError("Request hat keinen lesbaren Body/Parameter (erwarte JSON mit timestamp_utc, latitude, longitude)")

def cusps_to_12(cusps):
    """Akzeptiert beide Varianten:
       - Länge 13 → Index 1..12
       - Länge 12 → Index 0..11
    """
    if not isinstance(cusps, (list, tuple)):
        raise ValueError("houses_ex lieferte keine Cusp-Liste")
    n = len(cusps)
    if n >= 13:
        return [round(float(cusps[i]), 3) for i in range(1, 13)]
    if n == 12:
        return [round(float(cusps[i]), 3) for i in range(0, 12)]
    raise ValueError(f"houses_ex lieferte unerwartete cusps-Länge: {n}")


# -------- API --------
# GET erlaubt, damit du im Browser testen kannst.
@app.route("/astro", methods=["POST", "GET"])
def astro():
    try:
        data = read_input()
        if not isinstance(data, dict):
            raise ValueError("Body muss JSON-Objekt sein")

        ts = data.get("timestamp_utc")
        lat = data.get("latitude")
        lon = data.get("longitude")
        hs_in = data.get("houses_system")  # optional: "P","W",...

        if lat is None or lon is None:
            raise ValueError("latitude und longitude sind erforderlich")

        lat, lon = validate_lat_lon(lat, lon)
        hs_code = pick_housesys(hs_in)

        # Zeitpunkt -> Julian Day
        dt = parse_ts(ts)
        jd = swe.julday(dt.year, dt.month, dt.day,
                        dt.hour + dt.minute/60 + dt.second/3600)

        warnings = []

        # Häuser mit Fallback (bei Problemen auf Whole Sign)
        try:
            cusps, ascmc = swe.houses_ex(jd, lat, lon, hs_code)
        except Exception:
            cusps, ascmc = swe.houses_ex(jd, lat, lon, b"W")
            warnings.append("houses_system_fallback_to_W")
            hs_code = b"W"

        if not isinstance(ascmc, (list, tuple)) or len(ascmc) < 2:
            raise ValueError(f"houses_ex lieferte unerwartete ascmc-Länge: {len(ascmc) if isinstance(ascmc,(list,tuple)) else 'kein Array'}")

        asc = normalize_deg(ascmc[0])
        mc  = normalize_deg(ascmc[1])
        cusps12 = cusps_to_12(cusps)

        # Planeten robust
        planets = {}
        planet_result_lengths = {}
        for name, pid in PLANETS.items():
            try:
                res = swe.calc_ut(jd, pid, EPH_FLAGS)
                if not isinstance(res, (list, tuple)) or len(res) < 1:
                    planets[name] = {"error": "calc_ut Ergebnis leer/ungültig"}
                    planet_result_lengths[name] = None
                    warnings.append(f"{name}_calc_failed")
                    continue
                planet_result_lengths[name] = len(res)
                plon = normalize_deg(res[0])
                plat = float(res[1]) if len(res) > 1 else 0.0
                pspeed = float(res[3]) if len(res) > 3 else 0.0
                planets[name] = {
                    "lon": round(plon, 3),
                    "lat": round(plat, 3),
                    "speed": round(pspeed, 3),
                    "sign": sign_from_lon(plon)
                }
            except Exception as ex:
                planets[name] = {"error": str(ex)}
                warnings.append(f"{name}_calc_failed")

        debug = str(data.get("debug")).lower() == "true" if isinstance(data.get("debug"), str) else bool(data.get("debug"))
        out = {
            "datetime_utc": dt.replace(tzinfo=pytz.UTC).isoformat(),
            "settings": {"houses_system": hs_code.decode("ascii"), "flags": int(EPH_FLAGS)},
            "planets": planets,
            "houses": {"asc": round(asc, 3), "mc": round(mc, 3), "cusps": cusps12}
        }
        if warnings:
            out["warnings"] = warnings
        if debug:
            out["__debug"] = {
                "jd": jd,
                "ascmc_len": len(ascmc),
                "cusps_len": len(cusps),
                "ascmc_raw": [float(x) for x in ascmc],
                "planet_result_lengths": planet_result_lengths
            }

        return jsonify(out), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
