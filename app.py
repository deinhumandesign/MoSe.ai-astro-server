from flask import Flask, request, jsonify
import swisseph as swe
import pytz
from datetime import datetime

app = Flask(__name__)

PLANETS = {
    "sun": swe.SUN, "moon": swe.MOON, "mercury": swe.MERCURY, "venus": swe.VENUS,
    "mars": swe.MARS, "jupiter": swe.JUPITER, "saturn": swe.SATURN,
    "uranus": swe.URANUS, "neptune": swe.NEPTUNE, "pluto": swe.PLUTO,
    "chiron": swe.CHIRON, "true_node": swe.TRUE_NODE
}

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]

def sign_from_lon(lon):
    return SIGNS[int(lon // 30) % 12]

@app.route("/astro", methods=["POST"])
def astro():
    data = request.get_json(force=True)
    ts = data.get("timestamp_utc")
    lat = float(data["latitude"])
    lon = float(data["longitude"])

    # Zeit → Julian Day
    if isinstance(ts, (int, float)):
        dt = datetime.utcfromtimestamp(float(ts))
    else:
        dt = datetime.fromisoformat(ts.replace("Z","+00:00")).astimezone(pytz.UTC).replace(tzinfo=None)
    jd = swe.julday(dt.year, dt.month, dt.day, dt.hour + dt.minute/60 + dt.second/3600)

    flags = swe.FLAG_SPEED

    ascmc, cusps = swe.houses_ex(jd, lat, lon, b"P")  # Placidus
    asc, mc = ascmc[0], ascmc[1]

    planets = {}
    for name, pid in PLANETS.items():
        lonlat, speed = swe.calc_ut(jd, pid, flags)[:3], swe.calc_ut(jd, pid, flags)[3]
        plon, plat, pspeed = lonlat[0], lonlat[1], speed
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
            "cusps": [round(x, 3) for x in cusps]
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
