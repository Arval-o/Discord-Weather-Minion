import requests
import os
import json
import re
import math
import time
from datetime import datetime, timedelta
from shapely.geometry import Point, shape, MultiPolygon
from shapely.affinity import translate
from radar_generator import generate_radar_image

# config
BASE_URL = "https://mrms.ncep.noaa.gov/ProbSevere/PROBSEVERE/"
STATE_FILE = "probsevere_state.json"

WEBHOOK_URL = os.environ.get("PROBSEVERE_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
ROLE_ID = "1485401778962043021"

HOME_LAT = 43.4806
HOME_LON = -88.2250
HOME_POINT = Point(HOME_LON, HOME_LAT)

ALERT_BOX = HOME_POINT.buffer(0.072)

HOURS_TO_PROJECT = 1

THRESHOLD_TOR = 2
THRESHOLD_WIND = 5
THRESHOLD_HAIL = 5

def get_latest_probsevere_url():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(BASE_URL, headers=headers, timeout=10)
        r.raise_for_status()
        filenames = re.findall(r'href="(MRMS_PROBSEVERE_\d+_\d+\.json)"', r.text)
        if not filenames:
            return None
        return BASE_URL + filenames[-1]
    except Exception as e:
        print(f"Error finding latest data: {e}")
        return None

def fetch_probsevere(url):
    print(f"[{(datetime.utcnow() - timedelta(hours=4)).strftime('%I:%M %p')}] Downloading: {url}")
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Error downloading data: {e}")
        return None

def post_to_discord(payload, message_id=None, file_path=None):
    if WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        print("Webhook URL not set!")
        return None

    files = None
    if file_path and os.path.exists(file_path):
        files = {"file": ("radar.png", open(file_path, "rb"), "image/png")}
        payload["embeds"][0]["image"] = {"url": "attachment://radar.png"}

    try:
        if message_id:
            update_url = f"{WEBHOOK_URL}/messages/{message_id}"
            if files:
                # ONLY inject the attachments override when updating to prevent gallery buildup!
                payload["attachments"] = []
                r = requests.patch(update_url, files=files, data={"payload_json": json.dumps(payload)})
            else:
                r = requests.patch(update_url, json=payload)
            r.raise_for_status()
            return message_id
        else:
            url = WEBHOOK_URL + "?wait=true"
            if files:
                r = requests.post(url, files=files, data={"payload_json": json.dumps(payload)})
            else:
                r = requests.post(url, json=payload)
            r.raise_for_status()
            return r.json().get("id")
    except Exception as e:
        print(f"Error posting to Discord: {e}")
        return None

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"alerted_storms": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def get_rotation_class(az_shear):
    if az_shear >= 0.015: return "🔴 EXTREME"
    elif az_shear >= 0.010: return "🟠 STRONG"
    elif az_shear >= 0.005: return "🟡 MODERATE"
    else: return "🔵 WEAK"

def build_discord_embed(props, impact_text):
    prob_severe = int(props.get("ProbSevere", 0))
    prob_tor = int(props.get("ProbTor", 0))
    prob_wind = int(props.get("ProbWind", 0))
    prob_hail = int(props.get("ProbHail", 0))
    motion_e = float(props.get("MOTION_EAST", 0))
    motion_s = float(props.get("MOTION_SOUTH", 0))

    speed_knots = math.sqrt(motion_e**2 + motion_s**2)
    speed_mph = int(speed_knots * 1.15078)

    if prob_severe >= 70:
        title = "SEVERE STORM APPROACHING AREA"
    else:
        title = "POSSIBLE SEVERE STORM APPROACHING AREA"

    color = 0x808080
    if prob_tor >= THRESHOLD_TOR: color = 0xFF0000
    elif prob_wind >= THRESHOLD_WIND and prob_hail >= THRESHOLD_HAIL: color = 0xFFA500
    elif prob_hail >= THRESHOLD_HAIL: color = 0x00FF00
    elif prob_wind >= THRESHOLD_WIND: color = 0x0000FF

    embed = {
        "title": title,
        "description": "*Live tracking, updates every ~2 minutes.*\n\n"
                       f"**Impact Window:** {impact_text}\n"
                       f"**Storm Motion:** {speed_mph} mph",
        "color": color,
        "fields": [],
        "footer": {"text": f"Last updated: {(datetime.utcnow() - timedelta(hours=4)).strftime('%I:%M %p EST')}"}
    }

    minor_threats = []

    if prob_tor >= THRESHOLD_TOR:
        llaz = float(props.get("P98LLAZ", 0))
        mlaz = float(props.get("P98MLAZ", 0))
        mlat = float(props.get("MLAT", 0))
        mlon = float(props.get("MLON", 0))

        val = f"`Low-Level Rotation:` {get_rotation_class(llaz)} ({llaz} /s)\n"
        val += f"`Mid-Level Rotation:` {get_rotation_class(mlaz)} ({mlaz} /s)"

        # Add the exact TVS coordinate to Discord if one is tracked!
        if mlat != 0 and mlon != 0:
            val += f"\n`TVS Location:` {mlat:.4f}, {mlon:.4f}"

        embed["fields"].append({"name": f"🌪️ TORNADO THREAT: {prob_tor}%", "value": val, "inline": False})
    elif prob_tor >= 5:
        minor_threats.append(f"{prob_tor}% chance of a tornado")

    if prob_hail >= THRESHOLD_HAIL:
        mesh = props.get("MESH", "0")
        vil = props.get("VIL", "0")
        val = f"`Max Expected Size:` {mesh} inches\n`VIL Core:` {vil} kg/m²"
        embed["fields"].append({"name": f"🧊 HAIL THREAT: {prob_hail}%", "value": val, "inline": False})
    elif prob_hail >=10:
        minor_threats.append(f"{prob_hail}% chance of severe hail")

    if prob_wind >= THRESHOLD_WIND:
        dcape = props.get("DCAPE", "0")
        val = f"`Downdraft Potential (DCAPE):` {dcape} J/kg"
        embed["fields"].append({"name": f"💨 WIND THREAT: {prob_wind}%", "value": val, "inline": False})
    elif prob_wind >=15:
        minor_threats.append(f"{prob_wind}% chance of severe wind")

    lightning = props.get("FLASH_RATE", "0")
    minor_text = ""
    if minor_threats:
        minor_text = f"\n*Minor Threats: This storm also has a " + " and a ".join(minor_threats) + ".*"

    embed["fields"].append({
        "name": "⚡ Live Storm Vitality",
        "value": f"`Lightning:` {lightning} flashes/min{minor_text}",
        "inline": False
    })

    return embed

def process_storms(data):
    features = data.get("features", [])
    print(f"Tracking {len(features)} storm objects nationwide...")

    state = load_state()
    current_time = time.time()

    state["alerted_storms"] = {k: v for k, v in state["alerted_storms"].items()
                               if current_time - v.get("timestamp", 0) < 7200}

    for storm in features:
        props = storm.get("properties", {})
        storm_id = str(props.get("ID", "Unknown"))

        try:
            prob_tor = int(props.get("ProbTor", 0))
            prob_wind = int(props.get("ProbWind", 0))
            prob_hail = int(props.get("ProbHail", 0))
            motion_e = float(props.get("MOTION_EAST", 0))
            motion_s = float(props.get("MOTION_SOUTH", 0))
        except ValueError:
            continue

        if prob_tor >= THRESHOLD_TOR or prob_wind >= THRESHOLD_WIND or prob_hail >= THRESHOLD_HAIL:

            geom = storm.get("geometry", {})
            if not geom or geom.get("type") != "Polygon":
                continue

            current_footprint = shape(geom)
            current_center = current_footprint.centroid

            speed_deg_per_hour = math.sqrt(motion_e**2 + motion_s**2) / 60.0
            speed_deg_per_min = speed_deg_per_hour / 60.0

            delta_lat = -(motion_s / 60.0) * HOURS_TO_PROJECT
            lat_radians = math.radians(current_center.y)
            delta_lon = ((motion_e / 60.0) / math.cos(lat_radians)) * HOURS_TO_PROJECT

            future_footprint = translate(current_footprint, xoff=delta_lon, yoff=delta_lat)
            swept_swath = MultiPolygon([current_footprint, future_footprint]).convex_hull
            final_threat_area = swept_swath.buffer(0.05)

            if final_threat_area.intersects(ALERT_BOX):

                if speed_deg_per_min > 0:
                    dist_to_entry = current_footprint.distance(ALERT_BOX)
                    eta_mins = int(dist_to_entry / speed_deg_per_min)

                    if eta_mins == 0:
                        impact_text = "Currently impacting area"
                    else:
                        impact_text = f"Entering area in ~{eta_mins} minutes"
                else:
                    impact_text = "Unknown (stationary)"

                previous_alert = state["alerted_storms"].get(storm_id)
                msg_id = previous_alert.get("message_id") if previous_alert else None

                embed = build_discord_embed(props, impact_text)
                payload = {"content": f"<@&{ROLE_ID}>", "embeds": [embed]}

                # Generate radar matrix
                print(f"Generating 2x2 Radar Matrix for Storm {storm_id}...")
                image_path = generate_radar_image(props, geom, "radar.png")

                if msg_id:
                    print(f"Updating existing alert for Storm {storm_id}")
                    post_to_discord(payload, message_id=msg_id, file_path=image_path)
                else:
                    print(f"🚨 NEW ALERT for Storm {storm_id}")
                    msg_id = post_to_discord(payload, file_path=image_path)

                state["alerted_storms"][storm_id] = {
                    "timestamp": current_time,
                    "message_id": msg_id
                }

    save_state(state)

def bot_loop():
    print("Starting Continuous ProbSevere Loop...")
    last_processed_url = None

    while True:
        try:
            url = get_latest_probsevere_url()
            if url and url != last_processed_url:
                data = fetch_probsevere(url)
                if data:
                    process_storms(data)
                    last_processed_url = url
            else:
                print(f"[{(datetime.utcnow() - timedelta(hours=4)).strftime('%I:%M %p')}] No new update yet. Waiting...")
        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(60)

if __name__ == "__main__":
    bot_loop()
