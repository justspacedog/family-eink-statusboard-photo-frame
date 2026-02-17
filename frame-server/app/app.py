# -*- coding:utf8 -*-
from flask import Flask, send_file, render_template, request, redirect, url_for, jsonify
import yaml
import os
import io
import random
import numpy as np
import requests
import rawpy
from PIL import Image, ImageOps, ImageEnhance
from pillow_heif import register_heif_opener
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from statusboard import StatusBoard
from datetime import datetime, timedelta
from cpy import convert_image, load_scaled

app = Flask(__name__, template_folder="/app")
register_heif_opener()

weather_config = {
    "api_key": os.getenv("OWM_API_KEY"),
    "latitude": 51.51,
    "longitude": 13.74,
}

# ------------------------------------------------------------------
# DEFAULT CONFIG
# ------------------------------------------------------------------
DEFAULT_CONFIG = {
    "mode": "statusboard",
    "calendar": {
        "feeds": [
            {"name": "Familie", "url": "https://ics", "color": "yellow"}
        ]
    },
    "weather": {
        "api_key": os.getenv("OWM_API_KEY") or "",
        "provider": "owm",
        "latitude": 51.51,
        "longitude": 13.74,
        "temp_unit": "celsius",
    },
    "statusboard": {
        "rotation": 0,
        "dither_strength": 0.8,
        "padding_left": 5,
        "padding_right": 5,
        "padding_top": 5,
        "padding_bottom": 0,
        "language": "de",
        "show_current_temp": True,
        "show_suntime": True,
        "show_dwd_warnings": True,
        "dwd_use_owm_sun_times": False,
        "color_conditions": True,
        "summary_scope_temp": "day",
        "summary_scope_precip_prob": "day",
        "summary_scope_precip_rate": "day",
        "summary_scope_wind": "day",
        "summary_scope_sunshine": "day",
        "max_precip_mm": 5.0,
        "diagram_auto_precip_max": True,
        "diagram_display_hours": 72,
        "diagram_include_past_hours": False,
        "diagram_locale": True,
        "diagram_night_shading": True,
        "diagram_hour_markers": True,
        "battery_show_below": 20,
        "month_label_first_day": True,
        "calendar_show_moon": True,
        "agenda_relative_days": 2,
        "agenda_weekday_format": "dddd",
        "agenda_date_format": "DD.MM.YYYY",
        "title_format": "dddd, D. MMMM",
        "last_updated_format": "HH:mm",
        "show_last_updated": True,
        "show_weather_fallback_info": True,
        "show_dwd_warning_near_value": True,
    },
    "display": {"mode": "fill"},
    "immich": {
        "url": "http://URL:PORT",
        "album": "ALBUMNAME",
        "rotation": 270,
        "enhanced": 1.5,
        "contrast": 1.0,
        "strength": 1.0,
        "display_mode": "fill",
        "image_order": "random",
        "sleep_start_hour": 23,
        "sleep_start_minute": 0,
        "sleep_end_hour": 6,
        "sleep_end_minute": 0,
        "wakeup_interval": 60
    }
}

current_config = DEFAULT_CONFIG.copy()
battery_status = {"percent": None, "updated": None}

IMMICH_API_KEY = os.getenv("IMMICH_API_KEY")
IMMICH_HEADERS = {
    "Accept": "application/json",
    "x-api-key": IMMICH_API_KEY or "",
}
TRACKING_FILE = "/config/immich_tracking.txt"


def _load_downloaded_images(albumname: str):
    try:
        if not os.path.exists(TRACKING_FILE):
            open(TRACKING_FILE, "w").close()
        with open(TRACKING_FILE, "r+") as f:
            lines = f.readlines()
            if not lines or lines[0].strip() != albumname:
                f.seek(0)
                f.truncate()
                f.write(f"{albumname}\n")
                return set()
            return set(line.strip() for line in lines[1:] if line.strip())
    except Exception:
        return set()


def _save_downloaded_image(albumname: str, asset_id: str):
    try:
        if not os.path.exists(TRACKING_FILE):
            open(TRACKING_FILE, "w").close()
        with open(TRACKING_FILE, "r+") as f:
            lines = f.readlines()
            if not lines or lines[0].strip() != albumname:
                f.seek(0)
                f.truncate()
                f.write(f"{albumname}\n")
            else:
                f.seek(0, 2)
            f.write(f"{asset_id}\n")
    except Exception:
        pass


def _fetch_album_assets(base_url: str, albumname: str):
    albums = requests.get(f"{base_url}/api/albums", headers=IMMICH_HEADERS, timeout=10)
    if albums.status_code != 200:
        return None, None
    albumid = next((a["id"] for a in albums.json() if a.get("albumName") == albumname), None)
    if not albumid:
        return None, None
    album = requests.get(f"{base_url}/api/albums/{albumid}", headers=IMMICH_HEADERS, timeout=10)
    if album.status_code != 200:
        return None, None
    data = album.json()
    return albumid, data.get("assets", [])


def _select_asset(assets, image_order: str, albumname: str):
    if not assets:
        return None
    downloaded = _load_downloaded_images(albumname)
    if image_order == "newest":
        sorted_assets = sorted(
            assets,
            key=lambda x: x.get("exifInfo", {}).get("dateTimeOriginal", "1970-01-01T00:00:00"),
            reverse=True,
        )
        latest_id = sorted_assets[0]["id"]
        if not downloaded or latest_id not in downloaded:
            remaining = sorted_assets
        else:
            remaining = [a for a in sorted_assets if a["id"] not in downloaded]
    else:
        remaining = [a for a in assets if a["id"] not in downloaded]
        if not remaining:
            remaining = assets
    return remaining[0] if image_order == "newest" else random.choice(remaining)


def _process_immich_image(image: Image.Image, rotation: int, display_mode: str,
                          img_enhanced: float, img_contrast: float, strength: float) -> Image.Image:
    # Use cython helper for scaling to EPD size (800x480)
    image = ImageOps.exif_transpose(image)
    img = load_scaled(image, rotation, display_mode)
    enhanced_img = ImageEnhance.Color(img).enhance(img_enhanced)
    enhanced_img = ImageEnhance.Contrast(enhanced_img).enhance(img_contrast)
    output_img = convert_image(enhanced_img, dithering_strength=strength)
    return Image.fromarray(output_img, mode="RGB")


def render_immich_image(config):
    immich = config.get("immich", {})
    base_url = immich.get("url")
    albumname = immich.get("album")
    if not base_url or not albumname or not IMMICH_API_KEY:
        raise Exception("Immich not configured")

    _, assets = _fetch_album_assets(base_url, albumname)
    if not assets:
        raise Exception("No assets found")

    image_order = immich.get("image_order", "random")
    selected = _select_asset(assets, image_order, albumname)
    if not selected:
        raise Exception("No asset selected")

    asset_id = selected["id"]
    _save_downloaded_image(albumname, asset_id)

    resp = requests.get(f"{base_url}/api/assets/{asset_id}/original", headers=IMMICH_HEADERS, timeout=20)
    resp.raise_for_status()
    image_data = io.BytesIO(resp.content)
    original_path = selected.get("originalPath", "").lower()
    if original_path.endswith((".raw", ".dng", ".arw", ".cr2", ".nef")):
        with rawpy.imread(image_data) as raw:
            rgb = raw.postprocess(use_camera_wb=True, use_auto_wb=False)
            image = Image.fromarray(rgb)
    else:
        image = Image.open(image_data)

    rotation = int(immich.get("rotation", 0))
    display_mode = immich.get("display_mode", "fill")
    img_enhanced = float(immich.get("enhanced", 1.0))
    img_contrast = float(immich.get("contrast", 1.0))
    strength = float(immich.get("strength", 1.0))
    return _process_immich_image(image.convert("RGB"), rotation, display_mode, img_enhanced, img_contrast, strength)

def render_statusboard_image(config):
    status_config = {
        "config": {
            "size": (480, 800),
            # Internal padding disabled; applied after rotation in /download
            "padding_left": 0,
            "padding_right": 0,
            "padding_top": 0,
            "padding_bottom": 0,
            "fontsize": 20,
            "language": config.get("statusboard", {}).get("language", "de"),
            "week_starts_on": "Monday",
            "weeks": 5,
            "week_start_offset": -1,
            "agenda_days": 7,
            "date_format": "DD.MM.YY",
            "time_format": "HH:mm",
            "ical_feeds": config.get("calendar", {}).get("feeds", []),
            "statusboard": config.get("statusboard", {}),
            "weather": {
                "api_key": config.get("weather", {}).get("api_key") or weather_config["api_key"],
                "provider": config.get("weather", {}).get("provider", "owm"),
                "latitude": config.get("weather", {}).get("latitude", weather_config["latitude"]),
                "longitude": config.get("weather", {}).get("longitude", weather_config["longitude"]),
                "temp_unit": config.get("weather", {}).get("temp_unit", "celsius"),
            },
            "battery": {
                "percent": battery_status.get("percent"),
                "updated": battery_status.get("updated"),
            },
        }
    }

    module = StatusBoard(status_config)
    return module.generate_image()

# ------------------------------------------------------------------
# IMAGE PALETTE CONVERSION (EPF)
# ------------------------------------------------------------------
palette = [
    (0, 0, 0),
    (255, 255, 255),
    (255, 255, 0),
    (255, 0, 0),
    (0, 0, 255),
    (0, 255, 0)
]

def depalette_image(pixels, palette):
    palette_array = np.array(palette)
    diffs = np.sqrt(np.sum((pixels[:, :, None, :] - palette_array[None, None, :, :]) ** 2, axis=3))
    indices = np.argmin(diffs, axis=2)
    indices[indices > 3] += 1
    return indices

def convert_to_c_code_in_memory(image_data):
    """Convert image to raw hex stream (comma/newline delimited) for ESP32."""
    # Convert image data to numpy array
    pixels = np.array(image_data)
    
    # Process palette
    indices = depalette_image(pixels, palette)
    
    # Compress pixels
    height, width = indices.shape
    bytes_array = [
        (indices[y, x] << 4) | indices[y, x + 1] if x + 1 < width else (indices[y, x] << 4)
        for y in range(height)
        for x in range(0, width, 2)
    ]
    
    # Generate raw hex stream (no braces or identifiers)
    output = io.StringIO()

    for i, byte_value in enumerate(bytes_array):
        output.write(f"{byte_value:02X},")
        if (i + 1) % 16 == 0:
            output.write("\n")
    
    # Convert output to bytes
    result = output.getvalue().encode('utf-8')
    output_bytes = io.BytesIO(result)
    output_bytes.seek(0)
    
    return output_bytes

# ------------------------------------------------------------------
# CONFIG WATCHER
# ------------------------------------------------------------------
class ConfigFileHandler(FileSystemEventHandler):
    def __init__(self, path, callback):
        self.path = path
        self.callback = callback

    def on_modified(self, event):
        if event.src_path == self.path:
            with open(self.path) as f:
                cfg = yaml.safe_load(f)
            self.callback(cfg)

def update_app_config(cfg):
    global current_config
    current_config = cfg

# ------------------------------------------------------------------
# ROUTES
# ------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("settings"))

@app.route("/setting", methods=["GET", "POST"])
def settings():
    global current_config
    if request.method == "POST":
        current_config["mode"] = request.form.get("mode", current_config.get("mode", "statusboard"))
        current_config["display"]["mode"] = request.form.get("display_mode", current_config["display"].get("mode", "fill"))
        current_config["immich"]["url"] = request.form.get("url")
        current_config["immich"]["album"] = request.form.get("album")
        current_config["immich"]["rotation"] = int(request.form.get("rotation", current_config["immich"].get("rotation", 0)))
        current_config["immich"]["display_mode"] = request.form.get("display_mode", current_config["immich"].get("display_mode", "fill"))
        current_config["immich"]["image_order"] = request.form.get("image_order", current_config["immich"].get("image_order", "random"))
        current_config["immich"]["enhanced"] = float(request.form.get("enhanced", current_config["immich"].get("enhanced", 1.0)))
        current_config["immich"]["contrast"] = float(request.form.get("contrast", current_config["immich"].get("contrast", 1.0)))
        current_config["immich"]["strength"] = float(request.form.get("strength", current_config["immich"].get("strength", 1.0)))
        current_config["immich"]["sleep_start_hour"] = int(request.form.get("sleep_start_hour", current_config["immich"].get("sleep_start_hour", 23)))
        current_config["immich"]["sleep_start_minute"] = int(request.form.get("sleep_start_minute", current_config["immich"].get("sleep_start_minute", 0)))
        current_config["immich"]["sleep_end_hour"] = int(request.form.get("sleep_end_hour", current_config["immich"].get("sleep_end_hour", 6)))
        current_config["immich"]["sleep_end_minute"] = int(request.form.get("sleep_end_minute", current_config["immich"].get("sleep_end_minute", 0)))
        current_config["immich"]["wakeup_interval"] = int(request.form.get("wakeup_interval", 60))
        # Statusboard calendar feeds (table rows)
        names = request.form.getlist("calendar_name[]")
        urls = request.form.getlist("calendar_url[]")
        colors = request.form.getlist("calendar_color[]")
        feeds = []
        for name, url, color in zip(names, urls, colors):
            if name or url:
                feeds.append({"name": name, "url": url, "color": color or "black"})
        current_config.setdefault("calendar", {})["feeds"] = feeds

        # Weather settings
        # API key comes from environment; do not store in config
        current_config["weather"]["latitude"] = float(request.form.get("weather_lat", current_config["weather"].get("latitude", 0)))
        current_config["weather"]["longitude"] = float(request.form.get("weather_lon", current_config["weather"].get("longitude", 0)))
        current_config["weather"]["provider"] = request.form.get("weather_provider", current_config["weather"].get("provider", "owm"))
        current_config["weather"]["temp_unit"] = request.form.get("weather_temp_unit", current_config["weather"].get("temp_unit", "celsius"))
        # Statusboard settings
        current_config.setdefault("statusboard", {})
        current_config["statusboard"]["rotation"] = int(request.form.get("statusboard_rotation", current_config["statusboard"].get("rotation", 0)))
        current_config["statusboard"]["dither_strength"] = float(request.form.get("statusboard_dither_strength", current_config["statusboard"].get("dither_strength", 0.8)))
        current_config["statusboard"]["padding_left"] = int(request.form.get("statusboard_padding_left", current_config["statusboard"].get("padding_left", 8)))
        current_config["statusboard"]["padding_right"] = int(request.form.get("statusboard_padding_right", current_config["statusboard"].get("padding_right", 10)))
        current_config["statusboard"]["padding_top"] = int(request.form.get("statusboard_padding_top", current_config["statusboard"].get("padding_top", 5)))
        current_config["statusboard"]["padding_bottom"] = int(request.form.get("statusboard_padding_bottom", current_config["statusboard"].get("padding_bottom", 0)))
        current_config["statusboard"]["language"] = request.form.get("statusboard_language", current_config["statusboard"].get("language", "de"))
        current_config["statusboard"]["show_current_temp"] = request.form.get("statusboard_show_current_temp") == "on"
        current_config["statusboard"]["show_suntime"] = request.form.get("statusboard_show_suntime") == "on"
        current_config["statusboard"]["show_dwd_warnings"] = request.form.get("statusboard_show_dwd_warnings") == "on"
        current_config["statusboard"]["dwd_use_owm_sun_times"] = request.form.get("statusboard_dwd_use_owm_sun_times") == "on"
        current_config["statusboard"]["color_conditions"] = request.form.get("statusboard_color_conditions") == "on"
        current_config["statusboard"]["summary_scope_temp"] = request.form.get("statusboard_summary_scope_temp", current_config["statusboard"].get("summary_scope_temp", "day"))
        current_config["statusboard"]["summary_scope_precip_prob"] = request.form.get("statusboard_summary_scope_precip_prob", current_config["statusboard"].get("summary_scope_precip_prob", "day"))
        current_config["statusboard"]["summary_scope_precip_rate"] = request.form.get("statusboard_summary_scope_precip_rate", current_config["statusboard"].get("summary_scope_precip_rate", "day"))
        current_config["statusboard"]["summary_scope_wind"] = request.form.get("statusboard_summary_scope_wind", current_config["statusboard"].get("summary_scope_wind", "day"))
        current_config["statusboard"]["summary_scope_sunshine"] = request.form.get("statusboard_summary_scope_sunshine", current_config["statusboard"].get("summary_scope_sunshine", "day"))
        current_config["statusboard"]["max_precip_mm"] = float(request.form.get("statusboard_max_precip_mm", current_config["statusboard"].get("max_precip_mm", 5.0)))
        current_config["statusboard"]["diagram_auto_precip_max"] = request.form.get("statusboard_diagram_auto_precip_max") == "on"
        current_config["statusboard"]["diagram_display_hours"] = int(request.form.get("statusboard_diagram_display_hours", current_config["statusboard"].get("diagram_display_hours", 72)))
        current_config["statusboard"]["diagram_include_past_hours"] = request.form.get("statusboard_diagram_include_past_hours") == "on"
        if "statusboard_diagram_locale" in request.form:
            current_config["statusboard"]["diagram_locale"] = request.form.get("statusboard_diagram_locale") == "on"
        current_config["statusboard"]["diagram_night_shading"] = request.form.get("statusboard_diagram_night_shading") == "on"
        current_config["statusboard"]["diagram_hour_markers"] = request.form.get("statusboard_diagram_hour_markers") == "on"
        current_config["statusboard"]["battery_show_below"] = float(request.form.get("statusboard_battery_show_below", current_config["statusboard"].get("battery_show_below", 20)))
        current_config["statusboard"]["month_label_first_day"] = request.form.get("statusboard_month_label_first_day") == "on"
        current_config["statusboard"]["calendar_show_moon"] = request.form.get("statusboard_calendar_show_moon") == "on"
        current_config["statusboard"]["agenda_relative_days"] = int(request.form.get("statusboard_agenda_relative_days", current_config["statusboard"].get("agenda_relative_days", 2)))
        current_config["statusboard"]["agenda_weekday_format"] = request.form.get("statusboard_agenda_weekday_format", current_config["statusboard"].get("agenda_weekday_format", "dddd"))
        current_config["statusboard"]["agenda_date_format"] = request.form.get("statusboard_agenda_date_format", current_config["statusboard"].get("agenda_date_format", "DD.MM.YYYY"))
        current_config["statusboard"]["title_format"] = request.form.get("statusboard_title_format", current_config["statusboard"].get("title_format", "dddd, D. MMMM"))
        current_config["statusboard"]["last_updated_format"] = request.form.get("statusboard_last_updated_format", current_config["statusboard"].get("last_updated_format", "HH:mm"))
        current_config["statusboard"]["show_last_updated"] = request.form.get("statusboard_show_last_updated") == "on"
        current_config["statusboard"]["show_weather_fallback_info"] = request.form.get("statusboard_show_weather_fallback_info") == "on"
        current_config["statusboard"]["show_dwd_warning_near_value"] = request.form.get("statusboard_show_dwd_warning_near_value") == "on"
        with open("/config/config.yaml", "w") as f:
            yaml.safe_dump(current_config, f)
        # Reload from disk to ensure UI reflects saved values
        with open("/config/config.yaml") as f:
            update_app_config(yaml.safe_load(f))
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        config=current_config,
        battery_percentage=battery_status.get("percent") or 0.0,
    )


@app.route("/battery", methods=["POST"])
def battery():
    data = request.get_json(silent=True) or {}
    percent = data.get("percent")
    timestamp = data.get("timestamp")
    if percent is not None:
        battery_status["percent"] = float(percent)
        if timestamp:
            battery_status["updated"] = str(timestamp)
        else:
            battery_status["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    return {"ok": True, "percent": battery_status.get("percent"), "updated": battery_status.get("updated")}


@app.route("/battery", methods=["GET"])
def battery_get():
    return {"percent": battery_status.get("percent"), "updated": battery_status.get("updated")}


@app.route("/mode", methods=["GET"])
def mode_get():
    return {"mode": current_config.get("mode", "statusboard")}


@app.route("/mode", methods=["POST"])
def mode_set():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode in ("statusboard", "immich"):
        current_config["mode"] = mode
        with open("/config/config.yaml", "w") as f:
            yaml.safe_dump(current_config, f)
        return {"ok": True, "mode": mode}
    return {"ok": False, "error": "mode must be 'statusboard' or 'immich'"} , 400


@app.route("/sleep", methods=["GET"])
def get_sleep_duration():
    # Use system time
    current_time = datetime.now()

    # Get wake interval from config (in minutes)
    interval = int(current_config["immich"].get("wakeup_interval", 60))

    def calculate_next_interval_time(base_time, intervals=1):
        total_minutes = base_time.hour * 60 + base_time.minute
        next_total_minutes = interval * ((total_minutes // interval) + intervals)
        next_total_minutes = next_total_minutes % (24 * 60)
        next_time = base_time.replace(
            hour=next_total_minutes // 60,
            minute=next_total_minutes % 60,
            second=0,
            microsecond=0,
        )
        if next_time < base_time:
            next_time = next_time + timedelta(days=1)
        return next_time

    next_wakeup = calculate_next_interval_time(current_time)

    sleep_start = current_time.replace(
        hour=current_config["immich"].get("sleep_start_hour", 23),
        minute=current_config["immich"].get("sleep_start_minute", 0),
        second=0,
        microsecond=0,
    )
    sleep_end = current_time.replace(
        hour=current_config["immich"].get("sleep_end_hour", 6),
        minute=current_config["immich"].get("sleep_end_minute", 0),
        second=0,
        microsecond=0,
    )

    if sleep_end < sleep_start:
        if current_time >= sleep_start:
            sleep_end = sleep_end + timedelta(days=1)
        elif current_time < sleep_end:
            sleep_start = sleep_start - timedelta(days=1)

    if sleep_start <= next_wakeup < sleep_end:
        next_wakeup = sleep_end

    sleep_ms = int((next_wakeup - current_time).total_seconds() * 1000)
    if sleep_ms < 600000:
        next_wakeup = calculate_next_interval_time(current_time, intervals=2)
        if sleep_start <= next_wakeup < sleep_end:
            next_wakeup = sleep_end
        sleep_ms = int((next_wakeup - current_time).total_seconds() * 1000)

    return jsonify(
        {
            "current_time": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "next_wakeup": next_wakeup.strftime("%Y-%m-%d %H:%M:%S"),
            "sleep_duration": sleep_ms,
        }
    )

# ------------------------------------------------------------------
# DOWNLOAD ROUTE
# ------------------------------------------------------------------
@app.route("/download")
def download():
    # Capture battery voltage header from device (mV * 2 in Arduino)
    battery_mv = request.headers.get("batteryCap")
    if battery_mv:
        try:
            mv = float(battery_mv)
            # Simple linear estimate: 3.0V -> 0%, 4.2V -> 100%
            percent = max(0.0, min(100.0, (mv - 3000.0) / (4200.0 - 3000.0) * 100.0))
            battery_status["percent"] = percent
            battery_status["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        except Exception:
            pass

    mode = current_config.get("mode", "statusboard")
    if mode == "immich":
        try:
            image = render_immich_image(current_config)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        # Immich already returns 800x480 for epd7in3e
        if image.size != (800, 480):
            image = image.resize((800, 480))
    else:
        image = render_statusboard_image(current_config)
        # Ensure statusboard is 480x800 (portrait)
        if image.size != (480, 800):
            canvas = Image.new("RGB", (480, 800), "white")
            px = (480 - image.size[0]) // 2
            py = (800 - image.size[1]) // 2
            canvas.paste(image, (px, py))
            image = canvas

        # Apply padding in portrait space
        pad_left = int(current_config.get("statusboard", {}).get("padding_left", 5))
        pad_right = int(current_config.get("statusboard", {}).get("padding_right", 5))
        pad_top = int(current_config.get("statusboard", {}).get("padding_top", 5))
        pad_bottom = int(current_config.get("statusboard", {}).get("padding_bottom", 0))
        if any([pad_left, pad_right, pad_top, pad_bottom]):
            full = Image.new("RGB", (480, 800), "white")
            avail_w = max(1, 480 - pad_left - pad_right)
            avail_h = max(1, 800 - pad_top - pad_bottom)
            if image.size != (avail_w, avail_h):
                image = image.resize((avail_w, avail_h), Image.NEAREST)
            full.paste(image, (pad_left, pad_top))
            image = full

        # Rotate to display orientation (epd7in3e expects 800x480)
        image = image.rotate(90, expand=True)
        # Optional extra rotation (e.g., 180)
        sb_rot = int(current_config.get("statusboard", {}).get("rotation", 0))
        # Invert 0/180 so 0° matches correct physical orientation
        if sb_rot == 0:
            sb_rot = 180
        elif sb_rot == 180:
            sb_rot = 0
        if sb_rot in (90, 180, 270):
            image = image.rotate(sb_rot, expand=True)
        # Dithering to preserve greys/text
        strength = float(current_config.get("statusboard", {}).get("dither_strength", 0.8))
        image = Image.fromarray(convert_image(image, dithering_strength=strength), mode="RGB")

    # In C-Code konvertieren
    c_code_bytes = convert_to_c_code_in_memory(image)

    return send_file(
        c_code_bytes,
        mimetype="text/plain",
        as_attachment=True,
        download_name="image.c"
    )

    # PNG zurückgeben für Debug
    #bio = io.BytesIO()
    #image.save(bio, format="PNG")
    #bio.seek(0)

    #return send_file(
    #    bio,
    #    mimetype="image/png",
    #    as_attachment=True,
    #    download_name="calendar.png"
    #)

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    os.makedirs("/config", exist_ok=True)
    cfg_path = "/config/config.yaml"
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w") as f:
            yaml.safe_dump(DEFAULT_CONFIG, f)
    with open(cfg_path) as f:
        update_app_config(yaml.safe_load(f))
    observer = Observer()
    observer.schedule(ConfigFileHandler(cfg_path, update_app_config), "/config", recursive=False)
    observer.start()
    app.run(host="0.0.0.0", port=5000, use_reloader=False, threaded=True)

if __name__ == "__main__":
    main()
