from flask import Flask, request, jsonify
import swisseph as swe
import pytz
from datetime import datetime

app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200


PLANETS = {
    "sun": swe.SUN, "moon": swe.MOON, "mercury": swe.MERCURY, "venus": swe.VENUS,
    "mars": swe.MARS, "jupiter": swe.JUPITER, "saturn": swe.SATURN,
    "uranus": swe.URANUS, "neptune": swe.NEPTUNE, "pluto": swe.PLUTO,
    "chiron": swe.CHIRON, "true_node": swe.TRUE_NODE,
    "lilith": swe.OSCU_APOG   # Lilith (osculating apogee = „True Lilith“)
}

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

def sign_from_lon(lon: float) -> str:
    return SIGNS[int(lon // 30) % 12]


@app.route("/astro", methods=["POST"])
def astro():
    try:
        data = request.get_json(force=True)

        ts = data.get("timestamp_utc")
        if "latitude" not in data or "longitude" not in data:
            raise ValueError("latitude und longitude sind erforderlich")
        lat = float(data["latitude"])
        lon = float(data["longitude"])

        def parse_ts(ts_val):
            if ts_val is None:
                raise ValueError("timestamp_utc fehlt")
            if isinstance(ts_val, (int, float)) or (isinstance(ts_val, str) and ts_val.strip().replace('.', '', 1).isdigit()):
                return datetime.utcfromtimestamp(float(ts_val))
            if isinstance(ts_val, str):
                s = ts_val.strip().replace(" ", "T")
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    return dt
                return dt.astimezone(pytz.UTC).replace(tzinfo=None)
            raise ValueError("timestamp_utc hat ein nicht unterstütztes Format")

        dt = parse_ts(ts)
        jd = swe.julday(dt.year, dt.month, dt.day, dt.hour + dt.minute/60 + dt.second/3600)

        flags = swe.FLG_SWIEPH | swe.FLG_SPEED
        ascmc, cusps = swe.houses_ex(jd, lat, lon, b"P")
        asc, mc = ascmc[0], ascmc[1]

        planets = {}
        for name, pid in PLANETS.items():
            res = swe.calc_ut(jd, pid, flags)
            plon, plat, pspeed = res[0], res[1], res[3]
            planets[name] = {
                "lon": round(plon, 3),
                "lat": round(plat, 3),
                "speed": round(pspeed, 3),
                "sign": sign_from_lon(plon)
            }

        return jsonify({
            "datetime_utc": dt.replace(tzinfo=pytz.UTC).isoformat(),
            "planets": planets,
            "houses": {
                "asc": round(asc, 3),
                "mc": round(mc, 3),
                "cusps": [round(float(x), 3) for x in cusps]
            }
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

