import requests
import os
import json
import re
import math
import time
from datetime import datetime, timedelta
from shapely.geometry import Point, shape, MultiPolygon
from shapely.affinity import translate
from shapely.ops import nearest_points
from radar_generator import generate_radar_image

# config
BASE_URL = "https://mrms.ncep.noaa.gov/ProbSevere/PROBSEVERE/"
STATE_FILE = "probsevere_state.json"
WEBHOOK_URL = os.environ.get("PROBSEVERE_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
ROLE_ID = "1485401778962043021"

HOME_LAT = 40.6035
HOME_LON = -80.0536
HOME_POINT = Point(HOME_LON, HOME_LAT)
ALERT_BOX = HOME_POINT.buffer(0.06)
HOURS_TO_PROJECT = 1

THRESHOLD_TOR = 15
THRESHOLD_WIND = 50
THRESHOLD_HAIL = 30

def safe_float(val):
    try: return float(val)
    except (ValueError, TypeError): return 0.0

def get_compass_dir(motion_e, motion_s):
    if motion_e == 0 and motion_s == 0: return "Stationary"
    angle = math.degrees(math.atan2(-motion_s, motion_e))
    if angle < 0: angle += 360
    dirs = ["East", "East-Northeast", "Northeast", "North-Northeast", "North", "North-Northwest", "Northwest", "West-Northwest", "West", "West-Southwest", "Southwest", "South- Southwest", "South", "South-Southeast", "Southeast", "East-Southeast", "East"]
    return dirs[int(round((angle / 360.0) * 16)) % 16]

def haversine_distance(p1, p2):
    lon1, lat1, lon2, lat2 = p1.x, p1.y, p2.x, p2.y
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return 3958.8 * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

def get_latest_probsevere_url():
    try:
        r = requests.get(BASE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        filenames = re.findall(r'href="(MRMS_PROBSEVERE_\d+_\d+\.json)"', r.text)
        return BASE_URL + filenames[-1] if filenames else None
    except: return None

def post_to_discord(payload, message_id=None, file_path=None):
    if WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE": return None

    f = None
    files = None
    if file_path and os.path.exists(file_path):
        f = open(file_path, "rb")
        files = {"files[0]": ("radar.png", f, "image/png")}
        # The ProbSevere embed is always the last embed in the array
        payload["embeds"][-1]["image"] = {"url": "attachment://radar.png"}
        payload["attachments"] = [{"id": 0, "filename": "radar.png"}]

    try:
        if message_id:
            r = requests.patch(f"{WEBHOOK_URL}/messages/{message_id}", files=files, data={"payload_json": json.dumps(payload)})
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1.0)))
                if f: f.seek(0)
                requests.patch(f"{WEBHOOK_URL}/messages/{message_id}", files=files, data={"payload_json": json.dumps(payload)})
            return message_id
        else:
            r = requests.post(WEBHOOK_URL + "?wait=true", files=files, data={"payload_json": json.dumps(payload)})
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1.0)))
                if f: f.seek(0)
                r = requests.post(WEBHOOK_URL + "?wait=true", files=files, data={"payload_json": json.dumps(payload)})
            return r.json().get("id")
    except: return None
    finally:
        if f: f.close()

def load_state():
    try:
        with open(STATE_FILE, "r") as f: return json.load(f)
    except: return {"alerted_storms": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def estimate_tornado_winds(maxllaz):
    base_wind = (maxllaz * 2000.0 * 2.23694) * 1.6
    if base_wind < 50: return 0, 0
    return int(base_wind * 0.85), int(base_wind * 1.15)

def estimate_straight_line_winds(dcape, mean_wind_kts):
    total_gust = (math.sqrt(2 * max(0, dcape)) * 2.23694 * 0.6) + (mean_wind_kts * 1.15078 * 0.4)
    if total_gust < 30: return 0, 0
    return int(total_gust * 0.85), int(total_gust * 1.15)

def get_hail_object(mesh_inches):
    sizes = [(0.5, "Pea"), (0.75, "Marble"), (0.88, "Dime"), (1.0, "Quarter"), (1.25, "Half Dollar"), (1.5, "Ping Pong Ball"), (1.75, "Golf Ball"), (2.0, "Hen Egg"), (2.5, "Tennis Ball"), (2.75, "Baseball"), (3.0, "Teacup"), (4.0, "Grapefruit")]
    for limit, name in sizes:
        if mesh_inches < limit: return name
    return "Softball"

def build_discord_embed(props, impact_text, storm_id):
    prob_tor, prob_wind, prob_hail = int(props.get("ProbTor", 0)), int(props.get("ProbWind", 0)), int(props.get("ProbHail", 0))
    speed_mph = int(math.sqrt(safe_float(props.get("MOTION_EAST", 0))**2 + safe_float(props.get("MOTION_SOUTH", 0))**2) * 1.15078)
    color = 0xFF0000 if prob_tor >= THRESHOLD_TOR else (0x00FF00 if prob_hail >= THRESHOLD_HAIL else 0x0000FF)

    embed = {
        "author": {"name": f"🔴 LIVE TELEMETRY: STORM OBJECT {storm_id}"},
        "description": f"**Storm ID:** `{storm_id}`\n"
                       f"**Threat Status:** {'🚨 **CRITICAL**' if int(props.get('ProbSevere', 0)) >= 70 else '⚠️ **ELEVATED**'}\n"
                       f"**Impact ETA:** {impact_text}\n"
                       f"**Storm Velocity:** {speed_mph} mph\n"
                       f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "color": color, "fields": [], "footer": {"text": "NCEP MRMS Live Pipeline"}
    }

    if prob_tor >= THRESHOLD_TOR:
        llaz = safe_float(props.get("MAXLLAZ", 0))
        val = f"`Low-Level Rotation:` {llaz:.4f} /s\n`Effective Bulk Shear:` {safe_float(props.get('EBSHEAR', 0))} kts\n\n"
        low_t, high_t = estimate_tornado_winds(llaz)
        if prob_tor >= 40 and low_t >= 70: val += f"**🌪️ EST. TORNADO WINDS: {low_t} - {high_t} mph**\n"
        mlat, mlon = safe_float(props.get("MLAT", 0)), safe_float(props.get("MLON", 0))
        if mlat != 0 and mlon != 0 and prob_tor >= 40: val += f"`TVS Coordinates:` {mlat:.4f}, {mlon:.4f}"
        embed["fields"].append({"name": f"🌪️ TORNADO THREAT: {prob_tor}%", "value": val, "inline": False})

    if prob_wind >= THRESHOLD_WIND:
        dcape, mean_wind = safe_float(props.get("DCAPE", 0)), safe_float(props.get("MEANWIND_1-3kmAGL", 0))
        val = f"`DCAPE:` {dcape} J/kg\n\n"
        low_w, high_w = estimate_straight_line_winds(dcape, mean_wind)
        if low_w >= 45: val += f"**💨 EST. SURFACE GUSTS: {low_w} - {high_w} mph**"
        embed["fields"].append({"name": f"💨 WIND THREAT: {prob_wind}%", "value": val, "inline": False})

    if prob_hail >= THRESHOLD_HAIL:
        mesh = safe_float(props.get("MESH", 0))
        val = f"`Max Expected Size:` **{mesh}\"** *(Size of a {get_hail_object(mesh)})*\n\n`VIL (Core Mass):` {safe_float(props.get('VIL', 0))} kg/m²"
        embed["fields"].append({"name": f"🧊 HAIL THREAT: {prob_hail}%", "value": val, "inline": False})

    return embed

def process_storms(data):
    features = data.get("features", [])
    state = load_state()
    current_time = time.time()

    current_threatening_ids = set()
    all_mrms_storms = {}

    for storm in features:
        props = storm.get("properties", {})
        storm_id = str(props.get("ID", "Unknown"))
        all_mrms_storms[storm_id] = storm

        try:
            prob_tor, prob_wind, prob_hail = int(props.get("ProbTor", 0)), int(props.get("ProbWind", 0)), int(props.get("ProbHail", 0))
            motion_e, motion_s = float(props.get("MOTION_EAST", 0)), float(props.get("MOTION_SOUTH", 0))
        except ValueError: continue

        if prob_tor >= THRESHOLD_TOR or prob_wind >= THRESHOLD_WIND or prob_hail >= THRESHOLD_HAIL:
            geom = storm.get("geometry", {})
            if not geom or 'Polygon' not in geom.get("type"): continue

            if geom['type'] == 'Polygon': exterior = geom['coordinates'][0]
            else: exterior = geom['coordinates'][0][0]

            current_footprint = shape(geom)
            speed_mph = math.sqrt(motion_e**2 + motion_s**2) * 1.15078

            delta_lat = -(motion_s / 60.0) * HOURS_TO_PROJECT
            delta_lon = ((motion_e / 60.0) / math.cos(math.radians(current_footprint.centroid.y))) * HOURS_TO_PROJECT
            swept_swath = MultiPolygon([current_footprint, translate(current_footprint, xoff=delta_lon, yoff=delta_lat)]).convex_hull

            if swept_swath.buffer(0.05).intersects(ALERT_BOX):
                current_threatening_ids.add(storm_id)
                if current_footprint.intersects(ALERT_BOX):
                    eta_mins = 0
                    impact_text = "Currently impacting area"
                else:
                    p1, p2 = nearest_points(current_footprint, ALERT_BOX)
                    dist_miles = haversine_distance(p1, p2)
                    eta_mins = int(dist_miles / (speed_mph / 60.0)) if speed_mph > 0 else 0
                    impact_text = f"Entering area in ~{eta_mins} minutes"

                msg_id = state["alerted_storms"].get(storm_id, {}).get("message_id")

                # Ensure we don't accidentally overwrite our Warnings Bot Piggyback Embeds!
                payload = {"embeds": [build_discord_embed(props, impact_text, storm_id)]}

                if msg_id:
                    try:
                        r = requests.get(f"{WEBHOOK_URL}/messages/{msg_id}")
                        if r.status_code == 200:
                            existing_msg = r.json()
                            payload["content"] = existing_msg.get("content", f"<@&{ROLE_ID}>")
                            warning_embeds = [e for e in existing_msg.get("embeds", []) if e.get("title", "").startswith("🚨 NWS")]
                            payload["embeds"] = warning_embeds + payload["embeds"]
                        else:
                            payload["content"] = f"<@&{ROLE_ID}>"
                    except:
                        payload["content"] = f"<@&{ROLE_ID}>"
                else:
                    payload["content"] = f"<@&{ROLE_ID}>"

                image_path = generate_radar_image(props, geom, "radar.png")
                new_msg_id = post_to_discord(payload, message_id=msg_id, file_path=image_path)

                if new_msg_id:
                    state["alerted_storms"][storm_id] = {
                        "timestamp": current_time,
                        "message_id": new_msg_id,
                        "eta_mins": eta_mins,
                        "status": "active",
                        # We save the polygon here so warnings_bot.py can find it!
                        "polygon": list(current_footprint.exterior.coords)
                    }

    # --- RESOLUTION / STOP-TRACKING SYSTEM ---
    for storm_id, data in list(state["alerted_storms"].items()):
        if data.get("status") == "active" and storm_id not in current_threatening_ids:
            msg_id = data["message_id"]
            eta_mins = data.get("eta_mins", 0)
            arrival_time = (datetime.utcnow() - timedelta(hours=4) + timedelta(minutes=eta_mins)).strftime('%I:%M %p')

            if storm_id in all_mrms_storms:
                props = all_mrms_storms[storm_id].get("properties", {})
                motion_e, motion_s = safe_float(props.get("MOTION_EAST", 0)), safe_float(props.get("MOTION_SOUTH", 0))
                prob_severe = int(props.get("ProbSevere", 0))

                if prob_severe > 30:
                    dir_str = get_compass_dir(motion_e, motion_s)
                    resolve_text = f"**Storm ID:** `{storm_id}`\n\n✅ This storm is no longer on track to hit the area. It has deviated and is currently moving {dir_str}."
                else:
                    resolve_text = f"**Storm ID:** `{storm_id}`\n\n✅ This storm no longer has a very high chance of being severe. Simply expect some rain and thunder at around {arrival_time}."
            else:
                resolve_text = f"**Storm ID:** `{storm_id}`\n\n✅ This storm no longer has a very high chance of being severe. Simply expect some rain and thunder at around {arrival_time}."

            payload = {
                "content": "",
                "embeds": [{"title": f"STORM {storm_id} TRACKING CONCLUDED", "description": resolve_text, "color": 0x555555}],
                "attachments": []
            }
            post_to_discord(payload, message_id=msg_id)
            state["alerted_storms"][storm_id]["status"] = "resolved"

    state["alerted_storms"] = {k: v for k, v in state["alerted_storms"].items() if current_time - v.get("timestamp", 0) < 14400}
    save_state(state)

def bot_loop():
    print("Starting Live MRMS Pipeline...")
    last_processed_url = None
    while True:
        try:
            url = get_latest_probsevere_url()
            if url and url != last_processed_url:
                data = fetch_probsevere(url)  # Ensure you define fetch_probsevere in your environment or use requests.get
                if data:
            process_storms(data)
            last_processed_url = url
            else: time.sleep(60)
        except Exception as e: print(f"Error in loop: {e}")

if __name__ == "__main__":
    bot_loop()
