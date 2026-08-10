import requests
import os
import json
import time
import re
from datetime import datetime
from shapely.geometry import Polygon, MultiPolygon, shape

# config
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_URL_HERE")
STATE_FILE = "alert_state.json"
URL = "https://api.weather.gov/alerts/active?area=PA"

TARGET_COUNTY = "Allegheny"
ROLE_ID = "1485401778962043021"  # Discord role for Severe Thunderstorm
MIN_LATITUDE = 40.55  # Optional: north of this latitude only

# load alerts
try:
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
except FileNotFoundError:
    state = {}

headers = {
    "User-Agent": "weather-bot (your-email@example.com)"
}

r = requests.get(URL, headers=headers)
if r.status_code != 200:
    print("Error fetching alerts")
    exit()

data = r.json()

def get_vtec(props):
    vtec_list = props.get("parameters", {}).get("VTEC", [])
    return vtec_list[0] if vtec_list else None

def get_alert_key(vtec):
    try:
        parts = vtec.split(".")
        if len(parts) >= 6:
            return ".".join(parts[3:6])
        return vtec
    except Exception:
        return vtec

def get_vtec_action(vtec):
    try:
        return vtec.split(".")[1]
    except Exception:
        return "NEW"

def discord_time(timestr):
    if not timestr:
        return "Unknown"
    dt = datetime.fromisoformat(timestr.replace("Z", "+00:00"))
    return f"<t:{int(dt.timestamp())}:F>"

def extract_warning_details(description):
    details = {}
    loc_match = re.search(r'(At \d{1,4} [AP]M [A-Z]{3},.*?moving .*?\.)', description, re.IGNORECASE | re.DOTALL)
    if loc_match:
        details['location'] = loc_match.group(1).replace('\n', ' ')

    haz_match = re.search(r'HAZARD\.\.\.(.*?)(?:\n[A-Z]|\Z)', description, re.DOTALL)
    if haz_match:
        details['hazard'] = haz_match.group(1).replace('\n', ' ').strip()

    src_match = re.search(r'SOURCE\.\.\.(.*?)(?:\n[A-Z]|\Z)', description, re.DOTALL)
    if src_match:
        details['source'] = src_match.group(1).replace('\n', ' ').strip()
    return details

def find_active_probsevere_storm(alert_geom):
    try:
        with open("probsevere_state.json", "r") as f:
            ps_state = json.load(f)
    except Exception:
        return None

    alert_poly = shape(alert_geom)

    for storm_id, data in ps_state.get("alerted_storms", {}).items():
        if data.get("status") == "active" and "polygon" in data:
            try:
                storm_poly = Polygon(data["polygon"])
                if alert_poly.intersects(storm_poly):
                    return data["message_id"]
            except Exception:
                continue
    return None

for alert in data.get("features", []):
    props = alert["properties"]
    vtec = get_vtec(props)
    if not vtec:
        continue
    expires = props.get("ends") or props.get("expires")
    message_type = props.get("messageType", "Alert")
    action = get_vtec_action(vtec)
    event = props.get("event", "")
    area = props.get("areaDesc", "")

    # county filter
    if TARGET_COUNTY not in area:
        continue

    # latitude filter
    geometry = alert.get("geometry")
    north_filter_pass = True
    if geometry and geometry.get("coordinates"):
        coords_list = []
        if geometry["type"] == "Polygon":
            coords_list = [pt for ring in geometry["coordinates"] for pt in ring]
        elif geometry["type"] == "MultiPolygon":
            coords_list = [pt for poly in geometry["coordinates"] for ring in poly for pt in ring]
        else:
            coords_list = [geometry["coordinates"]]

        north_filter_pass = any(pt[1] >= MIN_LATITUDE for pt in coords_list)

    if not north_filter_pass:
        continue

    def is_pds(props):
        text = " ".join([
            props.get("headline") or "",
            props.get("description") or "",
            props.get("instruction") or ""
        ]).lower()
        return "particularly dangerous situation" in text

    headline = props.get("headline") or event
    description = " ".join((props.get("description") or "No description available.").split())[:2500]
    instruction = " ".join((props.get("instruction") or "No instructions provided.").split())[:1200]
    severity = props.get("severity") or "Unknown"

    event_lower = event.lower()
    alert_key = get_alert_key(vtec)
    existing = state.get(alert_key)
    pds = is_pds(props)

    color = 3447003
    emoji = "⚠️"
    ping_everyone = False
    ping_role = False
    pds_header = ""
    pds_footer = ""

    if pds:
        ping_everyone = True
        pds_header = "🚨 **THIS IS A PARTICULARLY DANGEROUS SITUATION!!!** 🚨\n\n"
        footer_text = f"ONCE AGAIN, THIS IS NOT A REGULAR {event.upper()}! AN ABNORMALLY SEVERE SITUATION FOR THIS AREA IS UNFOLDING!"
        if "warning" in event_lower:
            footer_text += " TAKE COVER NOW!!!"
        pds_footer = f"\n\n🚨 **{footer_text}** 🚨"

    if "tornado warning" in event_lower:
        color = 0xFF00FF if pds else 16711680
        emoji = "🌪️"
        ping_everyone = True
    elif "tornado watch" in event_lower:
        color = 0x8B0000 if pds else 0xF4C2C2
        emoji = "🌪️"
        ping_role = True
    elif "severe thunderstorm warning" in event_lower:
        color = 0xFF0000 if pds else 16776960
        emoji = "⛈️"
        ping_role = True
    elif "severe thunderstorm watch" in event_lower:
        color = 0xB8860B if pds else 0xC9D96C
        emoji = "⛅"
    elif "blizzard warning" in event_lower:
        color = 0x000000 if pds else 0xFF8C00
        emoji = "❄️"
        ping_everyone = True
    elif "snow" in event_lower and "blizzard" not in event_lower:
        color = 0xFFFFFF
        emoji = "❄️"
    elif "flash flood warning" in event_lower:
        color = 0xFFFF00 if pds else 65280
        emoji = "🌊"
        ping_role = True
    elif "flood warning" in event_lower:
        color = 0xFFFF00 if pds else 0x006400
        emoji = "🌊"
    elif "advisory" in event_lower:
        color = 0x3498DB
        emoji = "ℹ️"

    if ping_everyone:
        content = f"@everyone {emoji} **{event}**"
    elif ping_role:
        if ROLE_ID:
            content = f"<@&{ROLE_ID}> {emoji} **{event}**"
        else:
            content = f"@here {emoji} **{event}**"
    else:
        content = f"{emoji} **{event}**"

    radar_url = f"https://radar.weather.gov/ridge/standard/KPBZ_loop.gif?t={int(time.time())}"

    # Handle Updates
    if existing and action != "CAN":
        old_expire = existing.get("expires")
        if expires != old_expire:
            update_embed = {
                "title": f"The {event} has been extended.",
                "description": f"Previous expiration: {discord_time(old_expire)}\nNew expiration: {discord_time(expires)}",
                "color": color
            }
            requests.post(WEBHOOK_URL, json={"content": "", "embeds": [update_embed]})
            state[alert_key]["expires"] = expires
        continue

    # Handle Cancellations
    if existing and action == "CAN":
        cancel_embed = {
            "title": f"The {event} has been canceled.",
            "description": f"There is no more threat to the area.",
            "color": 0x808080
        }
        requests.post(WEBHOOK_URL, json={"content": "", "embeds": [cancel_embed]})
        if alert_key in state:
            del state[alert_key]
        continue

    # Check for ProbSevere Piggyback Match
    probsevere_msg_id = find_active_probsevere_storm(geometry) if geometry else None

    if probsevere_msg_id and action != "CAN" and not existing:
        details = extract_warning_details(props.get("description", ""))
        small_desc = ""
        if "location" in details: small_desc += f"*{details['location']}*\n\n"
        if "hazard" in details: small_desc += f"**HAZARD:** {details['hazard']}\n"
        if "source" in details: small_desc += f"**SOURCE:** {details['source']}\n"
        if instruction: small_desc += f"\n**INSTRUCTIONS:** {instruction}"

        small_warning_embed = {
            "title": f"🚨 NWS {event.upper()} ISSUED",
            "description": small_desc,
            "color": color
        }

        r = requests.get(f"{WEBHOOK_URL}/messages/{probsevere_msg_id}")
        if r.status_code == 200:
            existing_msg = r.json()
            embeds = existing_msg.get("embeds", [])

            # Don't duplicate if already added
            if not any(e.get("title", "").startswith("🚨 NWS") for e in embeds):
                embeds.insert(0, small_warning_embed)

                # Change the message content to highlight the incoming severe weather
                new_content = f"🚨 **SEVERE WEATHER APPROACHING - {event.upper()} ISSUED** 🚨"
                if ROLE_ID and (ping_everyone or ping_role):
                    new_content = f"<@&{ROLE_ID}> " + new_content

                requests.patch(f"{WEBHOOK_URL}/messages/{probsevere_msg_id}", json={"content": new_content, "embeds": embeds})
                print(f"Piggybacked {event} onto ProbSevere storm message!")

        state[alert_key] = {"event": event, "expires": expires}

    elif not existing:
        # Standard Giant Warning Embed (No ProbSevere storm found)
        embed = {
            "title": headline,
            "description": f"{pds_header}{description}{pds_footer}" if pds else description,
            "color": color,
            "fields": [
                {"name": "Severity", "value": severity, "inline": True},
                {"name": "Expires", "value": discord_time(expires), "inline": True},
                {"name": "Instructions", "value": instruction, "inline": False},
                {"name": "Radar", "value": "[Open Radar](https://radar.weather.gov/station/kpbz/standard)", "inline": False}
            ],
            "image": {"url": radar_url}
        }
        payload = {"content": content, "embeds": [embed]}
        response = requests.post(WEBHOOK_URL, json=payload)

        if response.status_code in (200, 204):
            print(f"Posted: {event}")
            state[alert_key] = {"event": event, "expires": expires}

# save posted IDs
with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)
