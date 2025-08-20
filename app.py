from flask import Flask, request, jsonify
import swisseph as swe
import pytz
from datetime import datetime

app = Flask(__name__)

# Optional: Falls du später Ephemeriden-Dateien beilegst:
# swe.set_ephe_path(".")

# ---- Health/Info ----
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "service": "MoSe.ai_astro_server",
        "swisseph": getattr(swe, "__version__", "unknown"),
        "status": "live"
    }), 200


# ---- Konfigurationen ----
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

# Stabile Flags: Swiss Ephemeris + Geschwindigkeiten
EPH_FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED

# erlaubte Häusersystem-Kürzel (Swiss Ephemeris Codes)
ALLOWED_HOUSES = {
    "P",  # Placidus (Standard)
    "K",  # Koch
    "E",  # Equal
    "W",  # Whole Sign
    "R",  # Regiomontanus
    "C",  # Campanus
    "B",  # Alcabitius
    "H",  # Azimuth/Horizontal
    "M",  # Morinus
    "T",  # Polich/Page
    "O",  # Porphyrius
}


# ---- Helper ----
def sign_from_lon(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]

def parse_ts(ts_val):
    """Nimmt Unix (int/float/String) oder ISO (mit/ohne Z/Whitespace). Gibt naive UTC-datetime zurück."""
    if ts_val is None:
        raise ValueError("timestamp_utc fehlt")
    # Unix?
    if isinstance(ts_val, (int, float)):
        return datetime.utcfromtimestamp(float(ts_val))
    if isinstance(ts_val, str):
        s = ts_val.strip()
        # reine Zahl?
        if s.replace('.', '', 1).isdigit():
            return datetime.utcfromtimestamp(float(s))
        # ISO aufräumen
        s = s.replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # ohne TZ als UTC interpretieren
            return dt
        return dt.astimezone(pytz.UTC).replace(tzinfo=None)
    raise ValueError("timestamp_utc hat ein nicht unterstütztes Format")

def validate_lat_lon(lat, lon):
    if not (-90.0 <= lat <= 90.0):
        raise ValueError("latitude außerhalb des gültigen Bereichs (-90..90)")
    # Swiss Ephemeris erwartet geographische Länge: Ost positiv, West negativ.
    if not (-180.0 <= lon <= 180.0):
        raise ValueError("longitude außerhalb des gültigen Bereichs (-180..180)")

def pick_housesys(val):
    if not val:
        return b"P"  # Default Placidus
    code = str(val).strip().upper()
    if code not in ALLOWED_HOUSES:
        raise ValueError(f"houses_system '{code}' wird nicht unterstützt")
    return code.encode("ascii")


# ---- API ----
@app.route("/astro", methods=["POST"])
def astro():
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            raise ValueError("Body muss JSON-Objekt sein")

        # Pflichtfelder
        ts = data.get("timestamp_utc")
        lat = data.get("latitude")
        lon = data.get("longitude")

        if lat is None or lon is None:
            raise ValueError("latitude und longitude sind erforderlich")

        # in float wandeln
        lat = float(lat)
        lon = float(lon)
        validate_lat_lon(lat, lon)

        # optional: Häusersystem
        house_sys = pick_housesys(data.get("houses_system"))

        # Zeitpunkt -> Julian Day
        dt = parse_ts(ts)
        jd = swe.julday(dt.year, dt.month, dt.day,
                        dt.hour + dt.minute/60 + dt.second/3600)

        # Häuser: ACHTUNG Reihenfolge von houses_ex = (cusps, ascmc)
        cusps, ascmc = swe.houses_ex(jd, lat, lon, house_sys)
        # ascmc: [ASC, MC, ARMC, Vertex, Equatorial Asc, Co-Asc W.Koch, Co-Asc Munkasey, Polar Asc]
        asc, mc = float(ascmc[0]), float(ascmc[1])

        # 12 Hausspitzen (Index 1..12 gültig)
        cusps12 = [round(float(cusps[i]), 3) for i in range(1, 13)]

        # Planeten robust (einige Punkte liefern ggf. keine Geschwindigkeiten)
        planets = {}
        for name, pid in PLANETS.items():
            res = swe.calc_ut(jd, pid, EPH_FLAGS)
            # Erwartet: [lon, lat, dist, speed_lon, ...] – aber es gibt Fälle mit weniger Einträgen
            plon = float(res[0])
            plat = float(res[1])
            pspeed = float(res[3]) if len(res) > 3 else 0.0
            planets[name] = {
                "lon": round(plon, 3),
                "lat": round(plat, 3),
                "speed": round(pspeed, 3),
                "sign": sign_from_lon(plon)
            }

        return jsonify({
            "datetime_utc": dt.replace(tzinfo=pytz.UTC).isoformat(),
            "settings": {
                "houses_system": house_sys.decode("ascii"),
                "flags": int(EPH_FLAGS)
            },
            "planets": planets,
            "houses": {
                "asc": round(asc, 3),
                "mc": round(mc, 3),
                "cusps": cusps12
            }
        }), 200

    except Exception as e:
        # Einheitliche, klare Fehlermeldung
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
