import base64
import hashlib
import json
import os
import time
from datetime import datetime
import zoneinfo
import feedparser
import requests
from shapely.geometry import MultiPolygon, Point, box, shape
from shapely.ops import nearest_points, unary_union

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
GH_TOKEN = os.environ["GH_TOKEN"]

REPO = "arval-o/Discord-Weather-Minion"
BRANCH = "main"
PAGE_FOLDER = "docs"

STATE_FILE = "state.json"
RSS_URL = "https://www.spc.noaa.gov/products/spcacrss.xml"

ROLE_ID = "1485401778962043021"
MY_ID = "1109224984984956968"

HOME_LON = -80.096278
HOME_LAT = 40.615111
POINT = Point(HOME_LON, HOME_LAT)
SAMPLE_RADIUS = 0.008

RISK_ORDER = ["NONE", "TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH"]
RISK_RANK = {risk: idx for idx, risk in enumerate(RISK_ORDER)}

MAPSERVER = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer"
LAYER_IDS = {
    "cat": {1: 1, 2: 9, 3: 17},
    "torn": {1: 3},
    "hail": {1: 5},
    "wind": {1: 7}
}

DN_TO_RISK = {2: "TSTM", 3: "MRGL", 4: "SLGT", 5: "ENH", 6: "MDT", 8: "HIGH"}

RISK_COLORS = {
    "NONE": 0x808080, "TSTM": 0x90EE90, "MRGL": 0x006400, "SLGT": 0xFFFF00,
    "ENH": 0xFFA500, "MDT": 0xFF0000, "HIGH": 0x8B0000,
}

RISK_EMOJIS = {
    "NONE": "⬜", "TSTM": "🟦", "MRGL": "🟩", "SLGT": "🟨",
    "ENH": "🟧", "MDT": "🟥", "HIGH": "⚠️",
}

DEFAULT_STATE = {
    "last_run_date": None,
    "day1_risk": None,
    "day2_risk": None,
    "day3_risk": None,
    "day1_key": None,
    "day2_key": None,
    "day3_key": None
}

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        for key, value in DEFAULT_STATE.items():
            state.setdefault(key, value)
        return state
    except Exception:
        return DEFAULT_STATE.copy()

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def upload_image(filename):
    img_response = requests.get(f"https://www.spc.noaa.gov/products/outlook/{filename}")
    if img_response.status_code != 200:
        return None
    with open(filename, "wb") as img_file:
        img_file.write(img_response.content)
    api = f"https://api.github.com/repos/{REPO}/contents/{PAGE_FOLDER}/{filename}"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    check_response = requests.get(api, headers=headers)
    sha = check_response.json().get("sha") if check_response.status_code == 200 else None
    with open(filename, "rb") as img_file:
        content_b64 = base64.b64encode(img_file.read()).decode()
    payload = {"message": f"update {filename}", "content": content_b64, "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    put_response = requests.put(api, headers=headers, data=json.dumps(payload))
    os.remove(filename)
    if put_response.status_code not in (200, 201):
        return None
    user, repo_name = REPO.split("/")
    return f"https://{user}.github.io/{repo_name}/{filename}?t={int(time.time())}"

def query_layer(layer_id):
    url = f"{MAPSERVER}/{layer_id}/query"
    params = {"where": "1=1", "outFields": "*", "f": "geojson"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("features", [])
    except Exception:
        return []

def geom_boundary(geom):
    if isinstance(geom, MultiPolygon):
        return unary_union([p.exterior for p in geom.geoms])
    return geom.exterior

def get_risk(day, point):
    sample_box = box(point.x - SAMPLE_RADIUS, point.y - SAMPLE_RADIUS,
                     point.x + SAMPLE_RADIUS, point.y + SAMPLE_RADIUS)
    cat_features = query_layer(LAYER_IDS["cat"][day])
    found = []
    for f in cat_features:
        try:
            geom = shape(f["geometry"])
            if geom.intersects(sample_box):
                dn = f["properties"].get("dn")
                risk_key = DN_TO_RISK.get(dn, "NONE")
                if risk_key != "NONE":
                    found.append(risk_key)
        except Exception:
            continue
    risk = "NONE"
    for r in reversed(RISK_ORDER):
        if r in found:
            risk = r
            break
    sub = {"tornado": 0, "wind": 0, "hail": 0, "sig": None}
    if day == 1:
        for prob_key, layer_map in [("tornado", LAYER_IDS["torn"]),
                                    ("hail",    LAYER_IDS["hail"]),
                                    ("wind",    LAYER_IDS["wind"])]:
            prob_features = query_layer(layer_map[1])
            for f in prob_features:
                try:
                    geom = shape(f["geometry"])
                    if geom.intersects(sample_box):
                        dn = f["properties"].get("dn", 0)
                        sub[prob_key] = max(sub[prob_key], int(dn) if dn else 0)
                        if int(dn or 0) >= 10:
                            sub["sig"] = True
                except Exception:
                    continue
    return risk, sub, None, found

def risk_change(old_risk, new_risk):
    if not old_risk:
        return None
    if RISK_RANK[new_risk] > RISK_RANK[old_risk]:
        return "upgrade"
    if RISK_RANK[new_risk] < RISK_RANK[old_risk]:
        return "downgrade"
    return "same"

def outlook_key(entry):
    if not entry:
        return None
    raw = f"{entry.title}|{entry.link}"
    return hashlib.sha256(raw.encode()).hexdigest()

def get_ping(risk):
    if risk in ["MDT", "HIGH"]:
        return "@everyone"
    elif risk in ["SLGT", "ENH"]:
        return f"<@&{ROLE_ID}>"
    return None

def main():
    state = load_state()

    tz = zoneinfo.ZoneInfo('US/Eastern')
    now = datetime.now(tz)
    today_str = now.strftime("%Y-%m-%d")

    feed = feedparser.parse(RSS_URL)
    entries = list(reversed(feed.entries))
    day1 = day2 = day3 = None
    for entry in entries:
        title = entry.title.lower()
        if "day 1" in title and day1 is None: day1 = entry
        elif "day 2" in title and day2 is None: day2 = entry
        elif "day 3" in title and day3 is None: day3 = entry

    day1_k = outlook_key(day1)
    day2_k = outlook_key(day2)
    day3_k = outlook_key(day3)

    is_morning_post = False
    if state["last_run_date"] != today_str:
        if now.hour > 6 or (now.hour == 6 and now.minute >= 30):
            is_morning_post = True
            state["last_run_date"] = today_str
        else:
            return

    if is_morning_post:
        r1, sub1, _, _ = get_risk(1, POINT)
        r2, _, _, _ = get_risk(2, POINT)
        r3, _, _, _ = get_risk(3, POINT)

        img1 = upload_image("day1otlk.png")
        img2 = upload_image("day2otlk.png")
        img3 = upload_image("day3otlk.png")

        embeds = []
        highest_risk = max([r1, r2, r3], key=lambda r: RISK_RANK[r])
        ping = get_ping(highest_risk)

        if day1 and img1:
            desc = f"**{RISK_EMOJIS.get(r1, '')} Risk: {r1}**\n"
            if r1 != "NONE" and (sub1["tornado"] or sub1["wind"] or sub1["hail"]):
                if sub1["tornado"]: desc += f"🌪️ Tornado: {sub1['tornado']}%\n"
                if sub1["wind"]: desc += f"💨 Wind: {sub1['wind']}%\n"
                if sub1["hail"]: desc += f"🧊 Hail: {sub1['hail']}%\n"
            embeds.append({"title": day1.title, "url": day1.link, "description": desc, "color": RISK_COLORS.get(r1, 0x808080), "image": {"url": img1}})

        if day2 and img2:
            embeds.append({"title": day2.title, "url": day2.link, "description": f"**{RISK_EMOJIS.get(r2, '')} Risk: {r2}**", "color": RISK_COLORS.get(r2, 0x808080), "thumbnail": {"url": img2}})

        if day3 and img3:
            embeds.append({"title": day3.title, "url": day3.link, "description": f"**{RISK_EMOJIS.get(r3, '')} Risk: {r3}**", "color": RISK_COLORS.get(r3, 0x808080), "thumbnail": {"url": img3}})

        state["day1_risk"] = r1
        state["day2_risk"] = r2
        state["day3_risk"] = r3
        state["day1_key"] = day1_k
        state["day2_key"] = day2_k
        state["day3_key"] = day3_k

        content = f"<@{MY_ID}>"
        if ping: content += f" {ping}"

        requests.post(f"{WEBHOOK_URL}?wait=true", json={"content": content, "embeds": embeds}, timeout=30)
        save_state(state)
        return

    # Check for upgrades
    embeds = []
    ping_content = ""

    def update_ping(current_ping, new_risk):
        if new_risk in ["MDT", "HIGH"]:
            return "@everyone"
        if current_ping != "@everyone":
            return f"<@&{ROLE_ID}>"
        return current_ping

    if day1_k and day1_k != state["day1_key"]:
        r1, sub1, _, _ = get_risk(1, POINT)
        if risk_change(state["day1_risk"], r1) == "upgrade":
            img1 = upload_image("day1otlk.png")
            if img1:
                desc = f"**{RISK_EMOJIS.get(r1, '')} Risk: {r1}** **(⚠️ UP FROM {state['day1_risk']})**\n"
                ping_content = update_ping(ping_content, r1)
                if RISK_RANK[r1] >= RISK_RANK["SLGT"]:
                    if sub1["tornado"]: desc += f"🌪️ Tornado: {sub1['tornado']}%\n"
                    if sub1["wind"]: desc += f"💨 Wind: {sub1['wind']}%\n"
                    if sub1["hail"]: desc += f"🧊 Hail: {sub1['hail']}%\n"
                    embeds.append({"title": day1.title, "url": day1.link, "description": desc, "color": RISK_COLORS.get(r1, 0x808080), "image": {"url": img1}})
                else:
                    embeds.append({"title": day1.title, "url": day1.link, "description": desc, "color": RISK_COLORS.get(r1, 0x808080), "thumbnail": {"url": img1}})
        state["day1_risk"] = r1
        state["day1_key"] = day1_k

    if day2_k and day2_k != state["day2_key"]:
        r2, _, _, _ = get_risk(2, POINT)
        if risk_change(state["day2_risk"], r2) == "upgrade" and RISK_RANK[r2] >= RISK_RANK["MRGL"]:
            img2 = upload_image("day2otlk.png")
            if img2:
                desc = f"**{RISK_EMOJIS.get(r2, '')} Risk: {r2}** **(⚠️ UP FROM {state['day2_risk']})**\n"
                ping_content = update_ping(ping_content, r2)
                embeds.append({"title": day2.title, "url": day2.link, "description": desc, "color": RISK_COLORS.get(r2, 0x808080), "thumbnail": {"url": img2}})
        state["day2_risk"] = r2
        state["day2_key"] = day2_k

    if day3_k and day3_k != state["day3_key"]:
        r3, _, _, _ = get_risk(3, POINT)
        if risk_change(state["day3_risk"], r3) == "upgrade" and RISK_RANK[r3] >= RISK_RANK["SLGT"]:
            img3 = upload_image("day3otlk.png")
            if img3:
                desc = f"**{RISK_EMOJIS.get(r3, '')} Risk: {r3}** **(⚠️ UP FROM {state['day3_risk']})**\n"
                ping_content = update_ping(ping_content, r3)
                embeds.append({"title": day3.title, "url": day3.link, "description": desc, "color": RISK_COLORS.get(r3, 0x808080), "thumbnail": {"url": img3}})
        state["day3_risk"] = r3
        state["day3_key"] = day3_k

    if embeds:
        content = f"<@{MY_ID}>"
        if ping_content: content += f" {ping_content}"
        requests.post(f"{WEBHOOK_URL}?wait=true", json={"content": content, "embeds": embeds}, timeout=30)

    save_state(state)

if __name__ == "__main__":
    main()
