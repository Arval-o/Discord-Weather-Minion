import os, requests, math, nexradaws, pyart, tempfile, shutil, time
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt
from datetime import datetime
from shapely.geometry import shape
import numpy as np

class CartoDBDark(cimgt.OSM):
    def _image_url(self, tile):
        x, y, z = tile
        return f'https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'

RADAR_CACHE_DIR = "/tmp/probsevere_radar_cache"
os.makedirs(RADAR_CACHE_DIR, exist_ok=True)
RADAR_CACHE = {}
CACHED_STATIONS = None

def get_closest_radar(lat, lon):
    global CACHED_STATIONS
    if CACHED_STATIONS is None:
        try: CACHED_STATIONS = requests.get('https://api.weather.gov/radar/stations', timeout=10).json()['features']
        except: return None

    closest, min_dist = None, 9999
    for s in CACHED_STATIONS:
        if s.get('properties', {}).get('stationType') != 'WSR-88D': continue
        r_lon, r_lat = s['geometry']['coordinates']
        dist = math.hypot(r_lat - lat, r_lon - lon)
        if dist < min_dist: min_dist, closest = dist, s['properties']['id']
    return closest

def download_latest_scan(radar_id):
    now = time.time()
    for rid, data in list(RADAR_CACHE.items()):
        if now - data['timestamp'] > 600:
            try: shutil.rmtree(data['temp_dir'])
            except: pass
            del RADAR_CACHE[rid]

    if radar_id in RADAR_CACHE and now - RADAR_CACHE[radar_id]['timestamp'] < 180:
        if os.path.exists(RADAR_CACHE[radar_id]['filepath']): return RADAR_CACHE[radar_id]['filepath']

    conn = nexradaws.NexradAwsInterface()
    utc = datetime.utcnow()
    try: scans = conn.get_avail_scans(utc.year, "{:02d}".format(utc.month), "{:02d}".format(utc.day), radar_id)
    except: return None

    valid_scans = [s for s in scans if not s.filename.endswith('_MDM')]
    if not valid_scans: return None

    temp_dir = tempfile.mkdtemp(dir=RADAR_CACHE_DIR)
    results = conn.download([valid_scans[-1]], temp_dir)
    if results.success:
        path = results.success[0].filepath
        RADAR_CACHE[radar_id] = {'timestamp': now, 'filepath': path, 'temp_dir': temp_dir}
        return path
    return None

def generate_radar_image(storm_props, storm_geom, output_path="radar_output.png"):
    storm_shape = shape(storm_geom)
    center = storm_shape.centroid
    lat, lon = center.y, center.x

    radar_id = get_closest_radar(lat, lon)
    if not radar_id: return None
    file_path = download_latest_scan(radar_id)
    if not file_path: return None

    try: radar = pyart.io.read(file_path)
    except: return None

    prob_tor, prob_hail = int(storm_props.get("ProbTor", 0)), int(storm_props.get("ProbHail", 0))
    try: mlat, mlon, maxllaz = float(storm_props.get("MLAT", 0)), float(storm_props.get("MLON", 0)), float(storm_props.get("MAXLLAZ", 0))
    except: mlat, mlon, maxllaz = 0, 0, 0

    if 'reflectivity' in radar.fields:
        ref_data = radar.fields['reflectivity']['data']
        radar.add_field_like('reflectivity', 'reflectivity_high', np.ma.masked_less(ref_data, 15), replace_existing=True)

    fig = plt.figure(figsize=(12, 10), facecolor='#1E1E1E')
    fig.suptitle(f"Telemetry Core Analysis: Storm {storm_props.get('ID', 'Unknown')}", fontsize=18, fontweight='bold', color='white', y=0.97)
    display = pyart.graph.RadarMapDisplay(radar)
    radar_proj = ccrs.LambertConformal(central_longitude=radar.longitude['data'][0], central_latitude=radar.latitude['data'][0])
    osm_tiles = CartoDBDark()

    motion_e, motion_s = float(storm_props.get("MOTION_EAST", 0)), float(storm_props.get("MOTION_SOUTH", 0))
    speed_kts = math.hypot(motion_e, motion_s)
    deg_lat_per_min, deg_lon_per_min = -(motion_s / 3600.0), (motion_e / 3600.0) / math.cos(math.radians(lat))

    if storm_geom.get('type') == 'Polygon': exterior = storm_geom['coordinates'][0]
    else: exterior = storm_geom['coordinates'][0][0]

    x, y = [c[0] for c in exterior], [c[1] for c in exterior]
    minx, miny, maxx, maxy = min(x), min(y), max(x), max(y)

    micro_w, micro_h = min(1.0, (maxx - minx) + 0.3), min(1.0, (maxy - miny) + 0.3)
    micro_bounds = [lon - micro_w/2, lon + micro_w/2, lat - micro_h/2, lat + micro_h/2]

    macro_w, macro_h = max(1.4, (maxx - minx) + 0.5), max(1.4, (maxy - miny) + 0.5)
    shift_lon = (motion_e / speed_kts) * (macro_w * 0.25) if speed_kts > 0 else 0
    shift_lat = (-motion_s / speed_kts) * (macro_h * 0.25) if speed_kts > 0 else 0
    macro_bounds = [lon - (macro_w/2) + shift_lon, lon + (macro_w/2) + shift_lon, lat - (macro_h/2) + shift_lat, lat + (macro_h/2) + shift_lat]

    def prep_ax(ax):
        ax.set_facecolor('#1E1E1E')
        ax.tick_params(colors='white')
        for spine in ax.spines.values(): spine.set_edgecolor('#555555')
        return ax

    ax1 = prep_ax(fig.add_subplot(221, projection=radar_proj))
    ax1.set_extent(macro_bounds, crs=ccrs.PlateCarree())
    ax1.add_image(osm_tiles, 8)
    display.plot_ppi_map('reflectivity_high' if 'reflectivity_high' in radar.fields else 'reflectivity',
                         0, vmin=15, vmax=64, ax=ax1, cmap='NWSRef', title=f"{radar_id} Base Reflectivity & Path",
                         min_lon=macro_bounds[0], max_lon=macro_bounds[1], min_lat=macro_bounds[2], max_lat=macro_bounds[3], resolution='50m', fig=fig, alpha=0.95)

    ax1.plot(x, y, color='white', linewidth=3, transform=ccrs.PlateCarree(), zorder=10)
    if speed_kts > 5:
        end_lon, end_lat = lon + (deg_lon_per_min * 60.0), lat + (deg_lat_per_min * 60.0)
        ax1.plot([lon, end_lon], [lat, end_lat], color='white', lw=2, transform=ccrs.PlateCarree(), zorder=20)
        for m in [30, 60]:
            x_m, y_m = lon + (deg_lon_per_min * m), lat + (deg_lat_per_min * m)
            ax1.plot(x_m, y_m, marker='x', color='white', markersize=8, markeredgewidth=2, transform=ccrs.PlateCarree(), zorder=21)
            ax1.text(x_m, y_m + 0.015, f"{int(m)}m", color='white', fontsize=10, fontweight='bold', transform=ccrs.PlateCarree(), zorder=22, bbox=dict(facecolor='black', alpha=0.7, edgecolor='none', pad=2))

    ax2 = prep_ax(fig.add_subplot(222, projection=radar_proj))
    ax2.set_extent(micro_bounds, crs=ccrs.PlateCarree())
    ax2.add_image(osm_tiles, 10)
    display.plot_ppi_map('reflectivity', 0, vmin=10, vmax=64, ax=ax2, cmap='NWSRef', title="Core Reflectivity", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)

    ax3 = prep_ax(fig.add_subplot(223, projection=radar_proj))
    ax3.set_extent(micro_bounds, crs=ccrs.PlateCarree())
    ax3.add_image(osm_tiles, 10)
    display.plot_ppi_map('velocity', 1, vmin=-40, vmax=40, ax=ax3, cmap='NWSVel', title="Core Velocity", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)

    ax4 = prep_ax(fig.add_subplot(224, projection=radar_proj))
    ax4.set_extent(micro_bounds, crs=ccrs.PlateCarree())
    ax4.add_image(osm_tiles, 10)

    try:
        if prob_tor >= 15 or maxllaz > 0.005:
            if 'cross_correlation_ratio' in radar.fields: display.plot_ppi_map('cross_correlation_ratio', 0, vmin=0.7, vmax=1.05, ax=ax4, cmap='plasma', title="Correlation Coefficient (Tornado Debris)", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)
            else: display.plot_ppi_map('spectrum_width', 1, vmin=0, vmax=15, ax=ax4, cmap='NWS_SPW', title="Spectrum Width (Rotation Turbulence)", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)
        elif prob_hail >= 30 or float(storm_props.get("MESH", 0)) > 1.0:
            if 'differential_reflectivity' in radar.fields: display.plot_ppi_map('differential_reflectivity', 0, vmin=-2, vmax=6, ax=ax4, cmap='nipy_spectral', title="Differential Reflectivity (Giant Hail)", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)
            else: display.plot_ppi_map('reflectivity', 1, vmin=10, vmax=64, ax=ax4, cmap='NWSRef', title="Mid-Level Reflectivity", min_lon=micro_bounds[0],max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)
        else:
            if 'spectrum_width' in radar.fields: display.plot_ppi_map('spectrum_width', 1, vmin=0, vmax=15, ax=ax4, cmap='turbo', title="Spectrum Width (Gust Front Turbulence)", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)
            else: display.plot_ppi_map('velocity', 1, vmin=-40, vmax=40, ax=ax4, cmap='NWSVel', title="Mid-Level Velocity (Wind)", min_lon=micro_bounds[0], max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)
    except Exception as e:
        display.plot_ppi_map('reflectivity', 1, vmin=10, vmax=64, ax=ax4, cmap='NWSRef', title="Fallback Mid-Level Reflectivity", min_lon=micro_bounds[0],max_lon=micro_bounds[1], min_lat=micro_bounds[2], max_lat=micro_bounds[3], resolution='50m', fig=fig, alpha=0.85)

    for ax in [ax2, ax3, ax4]:
        ax.plot([minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny], color='white', alpha=0.8, linewidth=2, transform=ccrs.PlateCarree(), zorder=10)

    if mlat != 0 and mlon != 0 and maxllaz > 0.001 and prob_tor >= 40:
        if (minx - 0.15) <= mlon <= (maxx + 0.15) and (miny - 0.15) <= mlat <= (maxy + 0.15):
            for ax in [ax1, ax2, ax3, ax4]:
                ax.plot(mlon, mlat, marker='v', color='magenta', markersize=14, markeredgecolor='white', markeredgewidth=1.5, transform=ccrs.PlateCarree(), zorder=30)
                ax.text(mlon, mlat + 0.02, "TVS", color='magenta', fontsize=11, fontweight='bold', transform=ccrs.PlateCarree(), zorder=31, bbox=dict(facecolor='black', alpha=0.8, edgecolor='white', pad=2))

    for ax in [ax1, ax2, ax3, ax4]: ax.title.set_color('white')

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    try: plt.savefig(output_path, dpi=120, bbox_inches='tight', facecolor='#1E1E1E')
    finally:
        plt.close(fig)
        try: del radar
        except: pass

    return output_path
