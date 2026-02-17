"""
Self-contained Statusboard renderer (weather + calendar + agenda).
Requires fonts in ./fonts and standard Python deps.
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date, timezone
from typing import Dict, List, Optional, Tuple

import arrow
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont
from tzlocal import get_localzone
import recurring_ical_events
from icalendar import Calendar

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")
OWM_ICON_CACHE = os.path.join(BASE_DIR, ".cache", "owm_icons")

FONTS = {
    "regular": os.path.join(FONT_DIR, "NotoSans-SemiCondensed.ttf"),
    "semibold": os.path.join(FONT_DIR, "NotoSans-SemiCondensedSemiBold.ttf"),
    "ui_bold": os.path.join(FONT_DIR, "NotoSansUI-Bold.ttf"),
    "weather": os.path.join(FONT_DIR, "weathericons-regular-webfont.ttf"),
}


@dataclass
class FeedConfig:
    name: str
    url: Optional[str]
    path: Optional[str]
    color: Tuple[int, int, int]
    is_meals: bool = False


def _parse_color(value: Optional[str]) -> Tuple[int, int, int]:
    if not value:
        return (0, 0, 0)
    value = value.strip().lower()
    named = {
        "black": (0, 0, 0),
        "white": (255, 255, 255),
        "red": (255, 0, 0),
        "blue": (0, 0, 255),
        "green": (0, 140, 0),
        "yellow": (255, 215, 0),
        "gray": (120, 120, 120),
        "grey": (120, 120, 120),
        "orange": (255, 140, 0),
        "meals": (140, 90, 0),
    }
    if value in named:
        return named[value]
    if value.startswith("#") and len(value) == 7:
        try:
            return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))
        except Exception:
            return (0, 0, 0)
    return (0, 0, 0)


def _load_font(key_or_path: str, size: int) -> ImageFont.FreeTypeFont:
    path = FONTS.get(key_or_path, key_or_path)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

def _normalize_language(value: Optional[str]) -> str:
    if not value:
        return "en"
    lang = value.strip()
    lower = lang.lower()
    aliases = {
        "deutsch": "de",
        "german": "de",
        "english": "en",
    }
    if lower in aliases:
        return aliases[lower]
    if len(lower) >= 2:
        if len(lower) > 2 and lower[2] in ("-", "_"):
            return lower[:2]
        if len(lower) == 2:
            return lower
    return lower

def _weekday_label(dt: datetime, locale: str) -> str:
    lower = (locale or "").lower()
    if lower.startswith("de"):
        de_days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        try:
            return de_days[dt.weekday()]
        except Exception:
            return "Mo"
    try:
        label = arrow.get(dt).format("dd", locale=locale)
    except Exception:
        label = ""
    if not label:
        try:
            label = arrow.get(dt).format("dd", locale="en")
        except Exception:
            label = ""
    return label or dt.strftime("%a")

def _capitalize_first(text: str) -> str:
    if not text:
        return text
    return text[0].upper() + text[1:]

def _meal_label(hour: int, language: str) -> str:
    if language.startswith("de"):
        if hour < 11:
            return "Frühstück"
        if hour < 16:
            return "Mittagessen"
        return "Abendessen"
    if hour < 11:
        return "Breakfast"
    if hour < 16:
        return "Lunch"
    return "Dinner"

def _draw_cutlery_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color):
    s = max(10, int(size))
    half = s // 2
    x0 = cx - half
    y0 = cy - half
    left = x0 + 2
    right = x0 + s - 3
    body_top = y0 + 4
    body_bottom = y0 + s - 1
    # Pot body outline.
    draw.rectangle((left, body_top, right, body_bottom), outline=color, width=1)
    # Lid + knob.
    draw.line((left + 1, body_top - 1, right - 1, body_top - 1), fill=color, width=1)
    draw.line((cx - 1, body_top - 2, cx + 1, body_top - 2), fill=color, width=1)
    # Handles.
    draw.line((left - 1, body_top + 1, left - 1, body_bottom - 1), fill=color, width=1)
    draw.line((right + 1, body_top + 1, right + 1, body_bottom - 1), fill=color, width=1)
    # Steam lines (mdi-inspired).
    steam_y0 = y0
    draw.line((cx - 2, steam_y0 + 1, cx - 2, body_top - 3), fill=color, width=1)
    draw.line((cx, steam_y0, cx, body_top - 3), fill=color, width=1)
    draw.line((cx + 2, steam_y0 + 1, cx + 2, body_top - 3), fill=color, width=1)

def _truncate_text(font: ImageFont.FreeTypeFont, text: str, max_w: int) -> str:
    if font.getbbox(text)[2] <= max_w:
        return text
    if max_w <= 0:
        return ""
    suffix = "..."
    trimmed = text
    while trimmed and font.getbbox(trimmed + suffix)[2] > max_w:
        trimmed = trimmed[:-1]
    return (trimmed + suffix) if trimmed else ""

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)

def _strip_emoji(text: str) -> str:
    if not text:
        return text
    return _EMOJI_RE.sub("", text).strip()


def _get_system_tz():
    try:
        return get_localzone()
    except Exception:
        return None


def _fetch_owm_icon(icon_code: str, size: int) -> Image.Image:
    os.makedirs(OWM_ICON_CACHE, exist_ok=True)
    iconpath = os.path.join(OWM_ICON_CACHE, f"{icon_code}.png")
    if not os.path.exists(iconpath):
        url = f"https://openweathermap.org/img/wn/{icon_code}@2x.png"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        with open(iconpath, "wb") as f:
            f.write(r.content)
    icon = Image.open(iconpath).convert("RGBA")
    return icon.resize((size, size))


def _get_weather_owm(api_key: str, lat: float, lon: float, lang: str):
    base = "https://api.openweathermap.org/data/2.5"
    params = f"lat={lat}&lon={lon}&appid={api_key}&units=Metric&lang={lang}"
    current = requests.get(f"{base}/weather?{params}", timeout=8).json()
    forecast = requests.get(f"{base}/forecast?{params}", timeout=8).json()["list"]
    return current, forecast


def _get_weather_owm_current(api_key: str, lat: float, lon: float, lang: str):
    base = "https://api.openweathermap.org/data/2.5"
    params = f"lat={lat}&lon={lon}&appid={api_key}&units=Metric&lang={lang}"
    return requests.get(f"{base}/weather?{params}", timeout=8).json()


def _map_dwd_icon(icon_name: str) -> str:
    name = (icon_name or "").lower()
    mapping = {
        "clear-day": "01d",
        "clear-night": "01n",
        "partly-cloudy-day": "02d",
        "partly-cloudy-night": "02n",
        "cloudy": "04d",
        "fog": "50d",
        "rain": "10d",
        "sleet": "09d",
        "snow": "13d",
        "hail": "13d",
        "thunderstorm": "11d",
        "wind": "03d",
    }
    return mapping.get(name, "03d")


def _localize_condition_text(text: Optional[str], language: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if language.startswith("de"):
        mapping = {
            "dry": "Trocken",
            "clear": "Klar",
            "clear sky": "Klar",
            "partly cloudy": "Teilweise bewolkt",
            "cloudy": "Bewoelkt",
            "overcast": "Bedeckt",
            "fog": "Nebel",
            "mist": "Dunst",
            "rain": "Regen",
            "drizzle": "Nieselregen",
            "sleet": "Schneeregen",
            "snow": "Schnee",
            "hail": "Hagel",
            "thunderstorm": "Gewitter",
            "wind": "Windig",
        }
        if low in mapping:
            return mapping[low]
    return raw[0].upper() + raw[1:] if raw else raw


def _sum_sunshine_hours(entries: List[Dict]) -> Optional[float]:
    values = [e.get("sunshine") for e in entries if e.get("sunshine") is not None]
    if not values:
        return None
    total = float(sum(values))
    vmax = float(max(values))
    # Normalize common sunshine units:
    # - seconds -> hours
    # - minutes -> hours
    # - fractional hours -> keep as-is
    if vmax > 120.0:
        return total / 3600.0
    if vmax > 1.5:
        return total / 60.0
    return total


def _extract_sunshine_value(row: Dict) -> Optional[float]:
    for key in ("sunshine", "sunshine_duration", "sunshine_minutes", "sunshine_hours"):
        val = row.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except Exception:
            continue
    return None


def _estimate_suntime_from_solar(entries: List[Dict]) -> Optional[float]:
    vals = []
    for e in entries:
        v = e.get("solar")
        if v is None:
            continue
        try:
            vals.append(float(v))
        except Exception:
            continue
    if not vals:
        return None
    total = sum(vals)
    if total <= 0:
        return 0.0
    # Empirical conversion from hourly solar energy to sunshine-hours.
    return max(0.0, total / 0.33)


def _format_suntime(hours: Optional[float], language: str) -> Optional[str]:
    if hours is None:
        return None
    h = max(0.0, float(hours))
    if h < 1.0:
        minutes = int(round(h * 60.0))
        if language.startswith("de"):
            return f"{minutes} min"
        return f"{minutes} m"
    if abs(h - round(h)) < 0.05:
        return f"{int(round(h))} h"
    value = f"{h:.1f}".rstrip("0").rstrip(".")
    if language.startswith("de"):
        value = value.replace(".", ",")
    return f"{value} h"


def _nice_upper_precip_bound(value: float) -> float:
    v = max(0.1, float(value))
    exp = math.floor(math.log10(v))
    scale = 10 ** exp
    frac = v / scale
    if frac <= 1.0:
        nice = 1.0
    elif frac <= 2.0:
        nice = 2.0
    elif frac <= 2.5:
        nice = 2.5
    elif frac <= 5.0:
        nice = 5.0
    else:
        nice = 10.0
    return nice * scale


def _parse_dt_maybe_local(value: Optional[str], tzinfo, tzname: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = arrow.get(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tzname)
        return dt.to(tzinfo).datetime
    except Exception:
        return None


def _sunrise_sunset_noaa(lat: float, lon: float, day: date, tzname: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    # Deterministic fallback when provider data does not include sunrise/sunset.
    def _calc(is_sunrise: bool) -> Optional[datetime]:
        zenith = math.radians(90.833)
        n = day.timetuple().tm_yday
        lng_hour = lon / 15.0
        t = n + (((6.0 if is_sunrise else 18.0) - lng_hour) / 24.0)
        m = (0.9856 * t) - 3.289
        l = m + (1.916 * math.sin(math.radians(m))) + (0.020 * math.sin(math.radians(2 * m))) + 282.634
        l = l % 360.0
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l))))
        ra = ra % 360.0
        l_quadrant = (math.floor(l / 90.0)) * 90.0
        ra_quadrant = (math.floor(ra / 90.0)) * 90.0
        ra = (ra + (l_quadrant - ra_quadrant)) / 15.0
        sin_dec = 0.39782 * math.sin(math.radians(l))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(zenith) - (sin_dec * math.sin(math.radians(lat)))) / (cos_dec * math.cos(math.radians(lat)))
        if cos_h > 1 or cos_h < -1:
            return None
        h = (360.0 - math.degrees(math.acos(cos_h))) if is_sunrise else math.degrees(math.acos(cos_h))
        h /= 15.0
        local_t = h + ra - (0.06571 * t) - 6.622
        ut = (local_t - lng_hour) % 24.0
        hh = int(ut)
        mm = int((ut - hh) * 60.0)
        ss = int(round((((ut - hh) * 60.0) - mm) * 60.0))
        dt_utc = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hh, minutes=mm, seconds=ss)
        return arrow.get(dt_utc).to(tzname).datetime

    return _calc(True), _calc(False)


def _get_weather_dwd(lat: float, lon: float, tzname: str):
    base = "https://api.brightsky.dev"
    local_day = arrow.now(tzname).floor("day")
    start = local_day.shift(days=-1).format("YYYY-MM-DD")
    last = local_day.shift(days=10).format("YYYY-MM-DD")
    params = f"lat={lat}&lon={lon}&date={start}&last_date={last}&tz={tzname}"
    data = requests.get(f"{base}/weather?{params}", timeout=10).json()
    weather_rows = data.get("weather", [])
    sources = data.get("sources", [])
    source_by_id = {s.get("id"): s for s in sources if s.get("id") is not None}
    return weather_rows, source_by_id


def _flatten_warning_dict(data: Dict) -> List[Dict]:
    if isinstance(data.get("warnings"), list):
        return data.get("warnings")
    if isinstance(data.get("alerts"), list):
        return data.get("alerts")
    if isinstance(data.get("alerts"), dict):
        out = []
        for _, v in data.get("alerts", {}).items():
            if isinstance(v, list):
                out.extend([x for x in v if isinstance(x, dict)])
        return out
    if isinstance(data.get("vorabInformation"), list):
        return data.get("vorabInformation")
    if isinstance(data.get("warnings"), dict):
        out = []
        for _, v in data.get("warnings", {}).items():
            if isinstance(v, list):
                out.extend([x for x in v if isinstance(x, dict)])
        vi = data.get("vorabInformation")
        if isinstance(vi, dict):
            for _, v in vi.items():
                if isinstance(v, list):
                    out.extend([x for x in v if isinstance(x, dict)])
        return out
    if isinstance(data.get("features"), list):
        out = []
        for f in data.get("features"):
            if isinstance(f, dict):
                out.append(f.get("properties", f))
        return out
    return []


def _get_dwd_warnings(lat: float, lon: float, station_name: Optional[str] = None, tzname: str = "Europe/Berlin") -> List[Dict]:
    base = "https://api.brightsky.dev"
    headers = {"Accept": "application/json", "User-Agent": "statusboard/1.0"}
    # Primary source per Bright Sky docs: /alerts
    for path in ("alerts", "warnings"):
        for params in (
            {"lat": lat, "lon": lon, "tz": tzname},
            {"lat": lat, "lon": lon},
        ):
            try:
                r = requests.get(f"{base}/{path}", params=params, headers=headers, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict):
                        parsed = _flatten_warning_dict(data)
                        if parsed:
                            return parsed
            except Exception:
                pass
    # Fallback to official DWD warning feed.
    try:
        r = requests.get("https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json", timeout=10)
        txt = r.text.strip()
        start = txt.find("(")
        end = txt.rfind(")")
        if start != -1 and end != -1 and end > start:
            txt = txt[start + 1:end]
        if txt.endswith(";"):
            txt = txt[:-1]
        data = requests.models.complexjson.loads(txt)
        warnings = _flatten_warning_dict(data if isinstance(data, dict) else {})
        if not warnings:
            return []
        if station_name:
            token = station_name.strip().lower()
            token_parts = [p for p in re.split(r"[^a-z0-9]+", token) if len(p) >= 3]
            filtered = []
            for w in warnings:
                hay = " ".join([
                    str(w.get("regionName") or w.get("region_name") or ""),
                    str(w.get("state") or ""),
                    str(w.get("areaDesc") or w.get("area_desc") or ""),
                    str(w.get("headline") or ""),
                    str(w.get("event") or ""),
                ]).lower()
                if token and token in hay:
                    filtered.append(w)
                    continue
                if any(p in hay for p in token_parts):
                    filtered.append(w)
            if filtered:
                return filtered
        return warnings
    except Exception:
        return []


def _parse_any_time(value: Optional[str], tzname: str) -> Optional[arrow.Arrow]:
    if not value:
        return None
    try:
        if isinstance(value, str) and value.strip().isdigit():
            value = float(value.strip())
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e12:
                return arrow.get(v / 1000.0)
            if v > 1e9:
                return arrow.get(v)
        a = arrow.get(value)
        if a.tzinfo is None:
            a = a.replace(tzinfo=tzname)
        return a
    except Exception:
        return None


def _warning_rank_and_color(w: Dict) -> Tuple[int, Tuple[int, int, int]]:
    default = (2, (220, 120, 0))
    raw = str(
        w.get("severity")
        or w.get("level")
        or w.get("warning_level")
        or w.get("warn_level")
        or w.get("warnLevel")
        or ""
    ).strip().lower()
    if not raw:
        return default
    try:
        n = int(float(raw))
        if n >= 3:
            return (3, (200, 0, 0))
        if n == 2:
            return (2, (220, 120, 0))
        return (1, (210, 170, 0))
    except Exception:
        pass
    if any(k in raw for k in ("extreme", "severe", "violett", "red", "rot")):
        return (3, (200, 0, 0))
    if any(k in raw for k in ("moderate", "orange")):
        return (2, (220, 120, 0))
    if any(k in raw for k in ("minor", "yellow", "gelb")):
        return (1, (210, 170, 0))
    return default


def _pick_warning_markers(warnings: List[Dict], now: arrow.Arrow, tzname: str) -> List[Tuple[Tuple[int, int, int], Optional[str]]]:
    if not warnings:
        return []
    ranked: List[Tuple[int, Tuple[int, int, int], Optional[str]]] = []
    for w in warnings:
        rank, color = _warning_rank_and_color(w)
        focus = _warning_focus_metric(w)
        ranked.append((rank, color, focus))
    ranked.sort(key=lambda x: x[0], reverse=True)
    # One marker per focus area (min/max/precip/wind), plus at most one generic marker.
    best_by_focus: Dict[str, Tuple[int, Tuple[int, int, int], Optional[str]]] = {}
    best_generic: Optional[Tuple[int, Tuple[int, int, int], Optional[str]]] = None
    for rank, color, focus in ranked:
        if focus in ("min_temp", "max_temp", "precip", "wind"):
            key = str(focus)
            if key not in best_by_focus:
                best_by_focus[key] = (rank, color, focus)
        else:
            if best_generic is None:
                best_generic = (rank, color, None)
    ordered = ["max_temp", "min_temp", "precip", "wind"]
    out: List[Tuple[Tuple[int, int, int], Optional[str]]] = []
    for k in ordered:
        if k in best_by_focus:
            _, c, f = best_by_focus[k]
            out.append((c, f))
    if best_generic is not None:
        _, c, f = best_generic
        out.append((c, f))
    return out


def _warning_focus_metric(w: Optional[Dict]) -> Optional[str]:
    if not isinstance(w, dict):
        return None
    text = " ".join([
        str(w.get("event_de") or ""),
        str(w.get("event_en") or ""),
        str(w.get("headline_de") or ""),
        str(w.get("headline_en") or ""),
        str(w.get("description_de") or ""),
        str(w.get("description_en") or ""),
        str(w.get("event") or ""),
        str(w.get("headline") or ""),
        str(w.get("description") or ""),
    ]).lower()
    tokens = set(re.findall(r"[a-zA-Zäöüß]+", text))

    # Match wind only on full words to avoid false positives like "windward".
    wind_words = {"wind", "sturm", "boe", "böe", "böen", "gust", "gusts"}
    if tokens & wind_words:
        return "wind"
    if any(k in text for k in ("regen", "rain", "niederschlag", "schnee", "snow", "hail", "hagel", "precip")):
        return "precip"
    if any(k in text for k in ("frost", "frier", "kalt", "cold", "glatteis", "ice", "minus")):
        return "min_temp"
    if any(k in text for k in ("hitze", "heat", "heiss", "heiß", "hot")):
        return "max_temp"
    # Default to precipitation-related focus for generic weather warnings.
    return "precip"


def _draw_alert_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color):
    s = max(10, int(size))
    p1 = (x + s // 2, y)
    p2 = (x, y + s - 1)
    p3 = (x + s - 1, y + s - 1)
    draw.polygon([p1, p2, p3], fill=color)
    cx = x + s // 2
    draw.line((cx, y + 3, cx, y + s - 5), fill=(0, 0, 0), width=1)
    draw.point((cx, y + s - 3), fill=(0, 0, 0))


def _parse_events(urls: List[str], files: List[str], start: arrow.Arrow, end: arrow.Arrow, tzinfo) -> List[Dict]:
    icals = []
    for url in urls:
        if not url:
            continue
        data = requests.get(url, timeout=10).text
        icals.append(Calendar.from_ical(data))
    for path in files:
        if not path:
            continue
        with open(path, "r") as f:
            icals.append(Calendar.from_ical(f.read()))

    fmt = lambda d: (d.year, d.month, d.day, d.hour, d.minute, d.second)
    t_start = fmt(start)
    t_end = fmt(end)

    events = []
    for ical in icals:
        for ev in recurring_ical_events.of(ical).between(t_start, t_end):
            begin = arrow.get(ev.get("DTSTART").dt)
            endt = arrow.get(ev.get("DTEND").dt)
            if tzinfo:
                begin = begin.to(tzinfo)
                endt = endt.to(tzinfo)
            events.append({
                "title": ev.get("SUMMARY", "").lstrip(),
                "begin": begin,
                "end": endt,
            })
    events.sort(key=lambda e: e["begin"])
    return events


class StatusBoard:
    def __init__(self, config: Dict):
        cfg = config.get("config", {})
        self.width, self.height = cfg.get("size", (480, 800))
        self.padding_left = cfg.get("padding_left", 5)
        self.padding_right = cfg.get("padding_right", 5)
        self.padding_top = cfg.get("padding_top", 5)
        self.padding_bottom = cfg.get("padding_bottom", 0)
        self.fontsize = int(cfg.get("fontsize", 20))
        self.language = _normalize_language(cfg.get("language", "de"))
        self.week_start = cfg.get("week_starts_on", "Monday")
        self.calendar_weeks = int(cfg.get("weeks", 5))
        self.week_start_offset = int(cfg.get("week_start_offset", -1))
        self.agenda_days = int(cfg.get("agenda_days", 7))
        self.date_format = cfg.get("date_format", "DD.MM.YY")
        self.time_format = cfg.get("time_format", "HH:mm")

        self.tzinfo = _get_system_tz()

        feeds = cfg.get("ical_feeds") or []
        self.feeds: List[FeedConfig] = []
        for feed in feeds:
            raw_color = str(feed.get("color", "")).strip().lower()
            is_meals = raw_color == "meals"
            self.feeds.append(
                FeedConfig(
                    name=feed.get("name", "Calendar"),
                    url=feed.get("url"),
                    path=feed.get("file"),
                    color=_parse_color(raw_color),
                    is_meals=is_meals,
                )
            )

        self.weather_conf = cfg.get("weather", {})
        self.battery_conf = cfg.get("battery", {})
        self.status_conf = cfg.get("statusboard", {})
        self.title_format = self.status_conf.get("title_format", "dddd, D. MMMM")
        self.last_updated_format = self.status_conf.get("last_updated_format", "HH:mm")
        self.show_last_updated = bool(self.status_conf.get("show_last_updated", True))
        self.show_weather_fallback_info = bool(self.status_conf.get("show_weather_fallback_info", True))
        self.show_current_temp = bool(self.status_conf.get("show_current_temp", True))
        self.show_suntime = bool(self.status_conf.get("show_suntime", True))
        self.show_dwd_warnings = bool(self.status_conf.get("show_dwd_warnings", True))
        self.dwd_use_owm_sun_times = bool(self.status_conf.get("dwd_use_owm_sun_times", False))
        self.color_conditions = bool(self.status_conf.get("color_conditions", True))
        self.summary_scope_temp = str(self.status_conf.get("summary_scope_temp", "day"))
        self.summary_scope_precip_prob = str(self.status_conf.get("summary_scope_precip_prob", "day"))
        self.summary_scope_precip_rate = str(self.status_conf.get("summary_scope_precip_rate", "day"))
        self.summary_scope_wind = str(self.status_conf.get("summary_scope_wind", "day"))
        self.summary_scope_sunshine = str(self.status_conf.get("summary_scope_sunshine", "day"))
        self.max_precip_mm = float(self.status_conf.get("max_precip_mm", 5.0))
        self.diagram_auto_precip_max = bool(self.status_conf.get("diagram_auto_precip_max", True))
        self.diagram_display_hours = int(self.status_conf.get("diagram_display_hours", 72))
        self.diagram_include_past_hours = bool(self.status_conf.get("diagram_include_past_hours", False))
        self.diagram_locale = bool(self.status_conf.get("diagram_locale", True))
        self.diagram_night_shading = bool(self.status_conf.get("diagram_night_shading", True))
        self.diagram_hour_markers = bool(self.status_conf.get("diagram_hour_markers", True))
        self.battery_show_below = float(self.status_conf.get("battery_show_below", 20))
        self.month_label_first_day = bool(self.status_conf.get("month_label_first_day", True))
        self.calendar_show_moon = bool(self.status_conf.get("calendar_show_moon", True))
        self.agenda_relative_days = int(self.status_conf.get("agenda_relative_days", 2))
        self.agenda_weekday_format = str(self.status_conf.get("agenda_weekday_format", "dddd"))
        self.agenda_date_format = str(self.status_conf.get("agenda_date_format", "DD.MM.YYYY"))
        self.show_dwd_warning_near_value = bool(self.status_conf.get("show_dwd_warning_near_value", True))
        self.weather_fallback_note: Optional[str] = None

    # -------- Weather helpers --------
    def _moon_phase_icon(self, now: arrow.Arrow) -> str:
        index = self._moon_phase_index(now)
        return {
            0: "\uf0eb",
            1: "\uf0d0",
            2: "\uf0d6",
            3: "\uf0d7",
            4: "\uf0dd",
            5: "\uf0de",
            6: "\uf0e4",
            7: "\uf0e5",
        }[int(index) & 7]

    def _moon_phase_index(self, now: arrow.Arrow) -> int:
        import decimal
        dec = decimal.Decimal
        diff = now - arrow.get(2001, 1, 1)
        days = dec(diff.days) + (dec(diff.seconds) / dec(86400))
        lunations = dec("0.20439731") + (days * dec("0.03386319269"))
        position = lunations % dec(1)
        return int(math.floor((position * dec(8)) + dec("0.5"))) & 7

    def _moon_phase_name(self, now: arrow.Arrow) -> str:
        index = self._moon_phase_index(now)
        names_de = [
            "Neumond",
            "zunehmende Sichel",
            "erstes Viertel",
            "zunehmender Mond",
            "Vollmond",
            "abnehmender Mond",
            "letztes Viertel",
            "abnehmende Sichel",
        ]
        names_en = [
            "New Moon",
            "Waxing Crescent",
            "First Quarter",
            "Waxing Gibbous",
            "Full Moon",
            "Waning Gibbous",
            "Last Quarter",
            "Waning Crescent",
        ]
        names = names_de if self.language.startswith("de") else names_en
        name = names[int(index) & 7]
        if self.language.startswith("de"):
            name = name.replace("zunehmende ", "zun. ")
            name = name.replace("zunehmender ", "zun. ")
            name = name.replace("abnehmende ", "abn. ")
            name = name.replace("abnehmender ", "abn. ")
        return name

    # -------- Calendar helpers --------
    def _calendar_start(self, now: arrow.Arrow) -> arrow.Arrow:
        if self.week_start == "Sunday":
            start = now.shift(days=-(now.isoweekday() % 7))
        else:
            start = now.shift(days=-now.weekday())
        if self.week_start_offset:
            start = start.shift(weeks=self.week_start_offset)
        return start

    def _is_all_day(self, event: Dict) -> bool:
        begin = event["begin"]
        end = event["end"]
        duration = end - begin
        if duration.days < 1 and duration.total_seconds() < 23 * 3600:
            return False
        begin_hm = begin.format("HH:mm")
        end_hm = end.format("HH:mm")
        if begin_hm in ("00:00", "01:00") and end_hm in ("00:00", "01:00"):
            return True
        if begin.date() != end.date() and begin.hour in (0, 1) and end.hour in (0, 1):
            return True
        return False

    def _load_events(self, start: arrow.Arrow, end: arrow.Arrow) -> List[Dict]:
        events: List[Dict] = []
        for feed in self.feeds:
            urls = [feed.url] if feed.url else []
            files = [feed.path] if feed.path else []
            feed_events = _parse_events(urls, files, start, end, self.tzinfo)
            for event in feed_events:
                event["feed_name"] = feed.name
                event["feed_color"] = feed.color
                event["is_meals"] = feed.is_meals
                events.append(event)
        events.sort(key=lambda e: e["begin"])
        title_counts: Dict[Tuple[str, str], int] = {}
        for e in events:
            key = (e.get("feed_name", ""), e.get("title", ""))
            title_counts[key] = title_counts.get(key, 0) + 1
        for e in events:
            key = (e.get("feed_name", ""), e.get("title", ""))
            e["is_recurring"] = title_counts.get(key, 0) > 1
        return events

    def _build_event_map(self, events: List[Dict]) -> Dict[date, List[Dict]]:
        event_map: Dict[date, List[Dict]] = {}
        for event in events:
            begin = event["begin"].floor("day")
            end = event["end"].floor("day")
            if self._is_all_day(event):
                end = end.shift(days=-1)
            cursor = begin
            while cursor <= end:
                event_map.setdefault(cursor.date(), []).append(event)
                cursor = cursor.shift(days=1)
        return event_map

    # -------- Drawing --------
    def _draw_header(self, draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], now: arrow.Arrow):
        x0, y0, x1, y1 = box
        font = _load_font("semibold", int(self.fontsize * 1.5))
        text = now.format(self.title_format, locale=self.language)
        w = font.getbbox(text)[2] - font.getbbox(text)[0]
        x = x0 + (x1 - x0 - w) // 2
        draw.text((x, y0), text, fill="black", font=font)

    def _build_weather_chart(
        self,
        size: Tuple[int, int],
        hourly: List[Dict],
        sunrise: Optional[datetime],
        sunset: Optional[datetime],
        marker_size: float = 5.0,
    ) -> Image.Image:
        w, h = size
        valid_hourly = [
            item for item in hourly
            if item.get("datetime") is not None and item.get("temp") is not None
        ]
        hourly = valid_hourly
        if not hourly:
            return Image.new("RGB", (max(1, w), max(1, h)), "white")
        timestamps = [item["datetime"] for item in hourly]
        temps = np.array([item.get("temp") for item in hourly])
        temps_min = np.array([item.get("min_temp") for item in hourly])
        temps_max = np.array([item.get("max_temp") for item in hourly])
        precip = np.array([item.get("precip_3h_mm") for item in hourly])

        dpi = 120
        fig, ax1 = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)
        ax1.set_facecolor("white")
        ax1.plot(timestamps, temps, marker="o", markersize=marker_size, linestyle="-", linewidth=2.4, color="#9b0019", zorder=3)
        ax1.fill_between(timestamps, temps_min, temps_max, color="#f0a8a8", alpha=0.6, linewidth=0, zorder=2)

        ax1.yaxis.set_major_locator(ticker.MaxNLocator(nbins=5))
        ax1.tick_params(axis="y", colors="#d62828", labelsize=8)
        ax1.set_ylabel("[°C]", color="#d62828", fontsize=8, labelpad=2)
        if len(temps_min) > 0 and len(temps_max) > 0:
            tmin = float(np.min(temps_min))
            tmax = float(np.max(temps_max))
            pad = 2.0
            if tmin == tmax:
                tmin -= pad
                tmax += pad
            ax1.set_ylim(tmin - pad, tmax + pad)
        # Guide line style: 00:00, 12:00 and 0°C.
        day_guide_color = "#6b6b6b"
        noon_guide_color = "#a3a3a3"
        day_guide_width = 1.1
        noon_guide_width = 0.9
        guide_alpha = 0.85
        noon_alpha = 0.62
        zero_color = "#8a3c3c"
        ax1.grid(True, axis="both", alpha=0.5, linewidth=1.0, color="#c7c7c7")

        ax2 = ax1.twinx()
        if len(timestamps) > 1:
            bar_width = np.min(np.diff(mdates.date2num(timestamps))) * 0.8
        else:
            bar_width = 0.03
        ax2.bar(timestamps, precip, color="#072c7a", width=bar_width, alpha=0.9, zorder=1)
        ax2.tick_params(axis="y", colors="#1d4ed8", labelsize=8)
        ax2.set_ylabel("[mm]", color="#1d4ed8", fontsize=8, labelpad=2)

        if self.diagram_auto_precip_max:
            pmax = float(np.nanmax(precip)) if len(precip) > 0 else 0.0
            max_mm = _nice_upper_precip_bound(max(1.0, pmax * 1.15))
        else:
            max_mm = max(1.0, self.max_precip_mm)
        ax2.set_ylim([0, max_mm])
        if abs(max_mm - 5.0) < 0.01:
            ax2.set_yticks([0, 2.5, 5.0])
        else:
            ax2.set_yticks([0, max_mm / 2, max_mm])

        ax1.xaxis.set_major_locator(mdates.DayLocator(interval=1))
        unique_days = sorted({t.date() for t in timestamps})
        data_tz = timestamps[0].tzinfo if timestamps else None
        tick_positions = [
            mdates.date2num(datetime(d.year, d.month, d.day, 0, 0, tzinfo=data_tz))
            for d in unique_days
        ]
        tick_labels = [
            _weekday_label(datetime(d.year, d.month, d.day), self.language)
            for d in unique_days
        ]
        ax1.xaxis.set_major_locator(ticker.FixedLocator(tick_positions))
        ax1.set_xticks(tick_positions)
        ax1.set_xticklabels(tick_labels)
        ax1.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=data_tz))
        if self.diagram_hour_markers:
            def _hour_minor_label(x, pos):
                dt = mdates.num2date(x, tz=data_tz)
                if dt.minute == 0 and dt.hour in (6, 12, 18):
                    return str(dt.hour)
                return ""
            ax1.xaxis.set_minor_formatter(ticker.FuncFormatter(_hour_minor_label))
            ax1.tick_params(axis="x", which="minor", labelsize=6, colors="#000000", labelbottom=True, pad=3)
        else:
            ax1.xaxis.set_minor_formatter(ticker.NullFormatter())
        ax1.tick_params(axis="x", which="major", labelsize=8, colors="#000000", labelbottom=True, pad=2)
        ax1.margins(x=0.02)

        # Day boundary lines and midday indicator lines
        data_min = min(timestamps) if timestamps else None
        data_max = max(timestamps) if timestamps else None
        data_tz = data_min.tzinfo if data_min else None
        unique_days = sorted({t.date() for t in timestamps})
        # Night shading (optional)
        if self.diagram_night_shading and sunrise and sunset:
            sr_h = sunrise.hour
            sr_m = sunrise.minute
            ss_h = sunset.hour
            ss_m = sunset.minute
            for d in unique_days:
                day_start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=data_tz)
                day_end = datetime(d.year, d.month, d.day, 23, 59, tzinfo=data_tz)
                sr = datetime(d.year, d.month, d.day, sr_h, sr_m, tzinfo=data_tz)
                ss = datetime(d.year, d.month, d.day, ss_h, ss_m, tzinfo=data_tz)
                if data_min and data_max:
                    if sr > day_start:
                        span_start = max(day_start, data_min)
                        span_end = min(sr, data_max)
                        if span_start < span_end:
                            ax1.axvspan(span_start, span_end, color="#c4c4c4", alpha=0.82, zorder=0)
                    if ss < day_end:
                        span_start = max(ss, data_min)
                        span_end = min(day_end, data_max)
                        if span_start < span_end:
                            ax1.axvspan(span_start, span_end, color="#c4c4c4", alpha=0.82, zorder=0)

        # Draw temporal guides after shading so all guides keep equal visual weight.
        for d in unique_days:
            day_start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=data_tz)
            if data_min and data_max and data_min <= day_start <= data_max:
                ax1.axvline(day_start, color=day_guide_color, linewidth=day_guide_width, alpha=guide_alpha, zorder=2.2)
            noon = datetime(d.year, d.month, d.day, 12, 0, tzinfo=data_tz)
            if data_min and data_max and data_min <= noon <= data_max:
                ax1.axvline(noon, color=noon_guide_color, linewidth=noon_guide_width, alpha=noon_alpha, zorder=2.2)

        # 0°C reference line.
        ax1.axhline(0.0, color=zero_color, linewidth=day_guide_width, alpha=guide_alpha, zorder=2.2)

        if data_min and data_max:
            ax1.set_xlim(data_min, data_max)

        fig.tight_layout(rect=[0.02, 0.00, 0.98, 1.00])
        buf = io.BytesIO()
        fig.savefig(buf, bbox_inches="tight", pad_inches=0, facecolor="white")
        buf.seek(0)
        img = Image.open(buf)
        plt.close(fig)
        return img

    def _draw_weather(self, base: Image.Image, box: Tuple[int, int, int, int]):
        x0, y0, x1, y1 = box
        api_key = self.weather_conf.get("api_key")
        lat = float(self.weather_conf.get("latitude"))
        lon = float(self.weather_conf.get("longitude"))
        provider = str(self.weather_conf.get("provider", "owm")).strip().lower()
        self.weather_fallback_note = None

        tzinfo = self.tzinfo
        now = arrow.now(tz=tzinfo)
        hourly = []
        summary_suntime_h = None
        summary_precip_rate_mmh = None
        dwd_warning_markers: List[Tuple[Tuple[int, int, int], Optional[str]]] = []
        current = None

        if provider == "dwd":
            try:
                # DWD data is Germany-centric; use Europe/Berlin for naive timestamps.
                tzname = "Europe/Berlin"
                rows, source_by_id = _get_weather_dwd(lat, lon, tzname)
                day_local = now.to(tzname).date()
                for r in rows:
                    ts = r.get("timestamp")
                    if not ts:
                        continue
                    dt = arrow.get(ts).to(tzinfo).datetime
                    temp = r.get("temperature")
                    precip_1h = float(r.get("precipitation", 0.0) or 0.0)
                    wind_kmh = float(r.get("wind_speed", 0.0) or 0.0)
                    icon_name = r.get("icon") or ""
                    hourly.append({
                        "temp": temp,
                        "min_temp": temp,
                        "max_temp": temp,
                        "precip_3h_mm": precip_1h * 3.0,
                        "wind": wind_kmh / 3.6,
                        "precip_probability": float(r.get("precipitation_probability", 0.0) or 0.0),
                        "icon": _map_dwd_icon(icon_name),
                        "datetime": dt,
                        "sunshine": _extract_sunshine_value(r),
                        "solar": r.get("solar"),
                        "condition": r.get("condition"),
                        "source_id": r.get("source_id"),
                    })
                hourly.sort(key=lambda h: h["datetime"])
                day_rows = [r for r in rows if r.get("timestamp") and arrow.get(r.get("timestamp")).to(tzname).date() == day_local]
                day_rows.sort(key=lambda r: r.get("timestamp", ""))
                # Use the closest record to now as current.
                if hourly:
                    current_like = min(hourly, key=lambda h: abs((h["datetime"] - now.datetime).total_seconds()))
                    sunrise = None
                    sunset = None
                    # DWD mode default: deterministic solar calculation.
                    # Optional override: use OWM if user enables it.
                    if self.dwd_use_owm_sun_times and api_key:
                        try:
                            owm_current = _get_weather_owm_current(api_key, lat, lon, self.language)
                            sunrise = datetime.fromtimestamp(owm_current["sys"]["sunrise"], tz=tzinfo)
                            sunset = datetime.fromtimestamp(owm_current["sys"]["sunset"], tz=tzinfo)
                        except Exception:
                            sunrise = None
                            sunset = None
                    if not sunrise or not sunset:
                        sr_calc, ss_calc = _sunrise_sunset_noaa(lat, lon, day_local, tzname)
                        sunrise = sunrise or sr_calc
                        sunset = sunset or ss_calc
                    current = {
                        "main": {"temp": current_like.get("temp")},
                        "wind": {"speed": current_like.get("wind", 0)},
                        "rain": {"1h": float(current_like.get("precip_3h_mm", 0.0) or 0.0) / 3.0},
                        "weather": [{"description": _localize_condition_text(current_like.get("condition"), self.language), "icon": current_like.get("icon", "03d")}],
                        "sys": {"sunrise": int(sunrise.timestamp()) if sunrise else int(now.shift(hours=-6).timestamp()),
                                "sunset": int(sunset.timestamp()) if sunset else int(now.shift(hours=6).timestamp())},
                    }
                # Day-based values for today in local timezone.
                sun_rows_day = [h for h in hourly if arrow.get(h["datetime"]).to(tzname).date() == day_local]
                sun_rows_24h = [h for h in hourly if now <= arrow.get(h["datetime"]) < now.shift(hours=24)]
                sun_rows = sun_rows_24h if self.summary_scope_sunshine == "24h" else sun_rows_day
                summary_suntime_h = _sum_sunshine_hours(sun_rows)
                day_precip = [float(r.get("precipitation", 0.0) or 0.0) for r in day_rows]
                next24_rows = [
                    r for r in rows
                    if r.get("timestamp")
                    and now <= arrow.get(r.get("timestamp")).to(tzinfo) < now.shift(hours=24)
                ]
                next24_precip = [float(r.get("precipitation", 0.0) or 0.0) for r in next24_rows]
                if self.summary_scope_precip_rate == "24h":
                    summary_precip_rate_mmh = max(next24_precip or [0.0])
                else:
                    summary_precip_rate_mmh = max(day_precip or [0.0])
                if self.show_dwd_warnings:
                    try:
                        station_name = None
                        if hourly:
                            sid = hourly[0].get("source_id")
                            src = source_by_id.get(sid) if sid is not None else None
                            if src:
                                station_name = src.get("station_name")
                        warnings = _get_dwd_warnings(lat, lon, station_name=station_name, tzname=tzname)
                        warn_src = "parsed"
                        if not warnings:
                            # Direct endpoint fallback (same shape as manual test URL).
                            try:
                                direct = requests.get(
                                    "https://api.brightsky.dev/alerts",
                                    params={"lat": lat, "lon": lon, "tz": "Europe/Berlin"},
                                    headers={"Accept": "application/json", "User-Agent": "statusboard/1.0"},
                                    timeout=10,
                                ).json()
                                if isinstance(direct, dict):
                                    parsed = _flatten_warning_dict(direct)
                                    if parsed:
                                        warnings = parsed
                                        warn_src = "direct_alerts"
                            except Exception:
                                warn_src = "direct_err"
                        dwd_warning_markers = _pick_warning_markers(warnings, now, tzname)
                    except Exception:
                        dwd_warning_markers = []
            except Exception:
                provider = "owm"
                hourly = []
                current = None
                self.weather_fallback_note = (
                    "Wetterquelle OWM (Fallback)" if self.language.startswith("de")
                    else "Weather Source OWM (fallback)"
                )
        if provider == "dwd" and (not hourly or current is None):
            provider = "owm"
            hourly = []
            current = None
            self.weather_fallback_note = (
                "Wetterquelle OWM (Fallback)" if self.language.startswith("de")
                else "Weather Source OWM (fallback)"
            )
        if provider != "dwd":
            current, forecast = _get_weather_owm(api_key, lat, lon, self.language)
            for f in forecast:
                dt = datetime.fromtimestamp(f["dt"], tz=tzinfo)
                rain = f.get("rain", {}).get("3h", 0.0)
                snow = f.get("snow", {}).get("3h", 0.0)
                hourly.append({
                    "temp": f["main"]["temp"],
                    "min_temp": f["main"]["temp_min"],
                    "max_temp": f["main"]["temp_max"],
                    "precip_3h_mm": rain + snow,
                    "wind": f["wind"]["speed"],
                    "precip_probability": f.get("pop", 0.0) * 100.0,
                    "icon": f["weather"][0]["icon"],
                    "datetime": dt,
                })

        # Summary datasets: per-day (default) or next 24h (optional per metric).
        local_day = now.date()
        day_set = [h for h in hourly if arrow.get(h["datetime"]).to(tzinfo).date() == local_day]
        next24_set = [h for h in hourly if now <= arrow.get(h["datetime"]) < now.shift(hours=24)]
        if not day_set:
            day_set = hourly[:8]
        if not next24_set:
            next24_set = hourly[:8]

        def _scope_set(scope: str) -> List[Dict]:
            return next24_set if scope == "24h" else day_set

        temps_set = _scope_set(self.summary_scope_temp)
        wind_set = _scope_set(self.summary_scope_wind)
        pop_set = _scope_set(self.summary_scope_precip_prob)
        precip_set = _scope_set(self.summary_scope_precip_rate)
        icon_set = temps_set

        temps = [h["temp"] for h in temps_set] or [current["main"]["temp"]]
        winds = [h["wind"] for h in wind_set] or [current["wind"]["speed"]]
        pops = [h.get("precip_probability", 0.0) for h in pop_set]
        precip_rates = [float(h.get("precip_3h_mm", 0.0) or 0.0) / 3.0 for h in precip_set]
        icons = [h.get("icon") for h in icon_set if h.get("icon")]

        sunrise_dt = datetime.fromtimestamp(current["sys"]["sunrise"], tz=tzinfo)
        sunset_dt = datetime.fromtimestamp(current["sys"]["sunset"], tz=tzinfo)
        # For OWM mode, prefer OWM sunrise/sunset values.
        try:
            if provider != "dwd" and api_key:
                owm_current = _get_weather_owm_current(api_key, lat, lon, self.language)
                sunrise_dt = datetime.fromtimestamp(owm_current["sys"]["sunrise"], tz=tzinfo)
                sunset_dt = datetime.fromtimestamp(owm_current["sys"]["sunset"], tz=tzinfo)
        except Exception:
            pass

        current_precip_rate = 0.0
        try:
            rain_curr = current.get("rain", {})
            if "1h" in rain_curr and rain_curr.get("1h") is not None:
                current_precip_rate = float(rain_curr.get("1h") or 0.0)
            elif "3h" in rain_curr and rain_curr.get("3h") is not None:
                current_precip_rate = float(rain_curr.get("3h") or 0.0) / 3.0
            elif precip_rates:
                current_precip_rate = float(precip_rates[0] or 0.0)
        except Exception:
            current_precip_rate = float(precip_rates[0] or 0.0) if precip_rates else 0.0
        if provider == "dwd" and summary_precip_rate_mmh is not None:
            current_precip_rate = float(summary_precip_rate_mmh)

        summary = {
            "icon": max(set(icons), key=icons.count) if icons else current["weather"][0]["icon"],
            "temp_now": current["main"]["temp"],
            "temp_min": min(temps),
            "temp_max": max(temps),
            "pop": max(pops) if pops else 0,
            "precip_rate_mmh": max(0.0, current_precip_rate),
            "wind": max(winds) if winds else current["wind"]["speed"],
            "sunrise": sunrise_dt,
            "sunset": sunset_dt,
            "status": _localize_condition_text(current["weather"][0]["description"], self.language),
            "sunshine_hours": summary_suntime_h,
            "dwd_warning_markers": dwd_warning_markers if (provider == "dwd" and self.show_dwd_warning_near_value) else [],
        }

        summary_h = int((y1 - y0) * 0.44)
        chart_h = (y1 - y0) - summary_h
        summary_box = (x0, y0, x1, y0 + summary_h)
        chart_box = (x0, y0 + summary_h, x1, y1)

        self._draw_weather_summary(base, summary_box, now, summary)
        chart_hourly = [h for h in hourly if h.get("datetime") is not None and h.get("temp") is not None]
        chart_hourly.sort(key=lambda h: h["datetime"])
        if chart_hourly:
            chart_tz = chart_hourly[0]["datetime"].tzinfo
            now_chart = now.to(chart_tz) if chart_tz else now
            data_min = chart_hourly[0]["datetime"]
            data_max = chart_hourly[-1]["datetime"]
            display_h = max(6, int(self.diagram_display_hours))
            if self.diagram_include_past_hours:
                chart_start = max(data_min, now_chart.floor("day").datetime)
            else:
                chart_start = max(data_min, now_chart.datetime)
            chart_end = min(data_max, chart_start + timedelta(hours=display_h))
            chart_hourly = [h for h in chart_hourly if chart_start <= h["datetime"] <= chart_end]
            if not chart_hourly:
                chart_hourly = [h for h in hourly if h.get("datetime") is not None and h.get("temp") is not None]
                chart_hourly.sort(key=lambda h: h["datetime"])
        else:
            chart_hourly = hourly
        marker_size = 2.0 if provider == "dwd" else 5.0
        chart = self._build_weather_chart(
            (x1 - x0, chart_h),
            chart_hourly,
            summary.get("sunrise"),
            summary.get("sunset"),
            marker_size=marker_size,
        )
        scale = 0.96
        scaled_w = max(1, int((x1 - x0) * scale))
        scaled_h = max(1, int(chart_h * scale))
        chart = chart.resize((scaled_w, scaled_h))
        dx = (x1 - x0 - scaled_w) // 2
        dy = (chart_h - scaled_h) // 2
        base.paste(chart, (x0 + dx, y0 + summary_h + dy))

    def _draw_weather_summary(self, base: Image.Image, box: Tuple[int, int, int, int], now: arrow.Arrow, summary: Dict):
        x0, y0, x1, y1 = box
        draw = ImageDraw.Draw(base)

        icon_font = _load_font("weather", int(self.fontsize * 2.0))
        label_font = _load_font("regular", int(self.fontsize * 0.78))
        value_font = _load_font("regular", int(self.fontsize * 1.15))
        side_value_font = _load_font("regular", int(self.fontsize * 1.0))
        side_note_font = _load_font("regular", int(self.fontsize * 0.72))
        status_small_font = _load_font("regular", int(self.fontsize * 0.82))
        temp_font = _load_font("ui_bold", int(self.fontsize * 1.2))
        arrow_font = _load_font("ui_bold", int(self.fontsize * 0.95))
        sunmoon_font = _load_font("weather", int(self.fontsize * 1.0))

        icon_x = x0 + 4
        line_gap = int(self.fontsize * 1.1)
        line1_y = y0 + 6
        line2_y = line1_y + line_gap
        line3_y = line2_y + line_gap
        left_raise = int(self.fontsize * 0.30)

        icon_color = (0, 0, 0)
        if self.color_conditions:
            icon_color = (60, 60, 60)
        precip_color = (0, 0, 0)
        wind_color = (0, 0, 0)
        sun_color = (0, 0, 0)
        moon_color = (0, 0, 0)
        if self.color_conditions:
            precip_color = (0, 80, 200)
            wind_color = (0, 120, 0)
            sun_color = (160, 60, 0)
            moon_color = (130, 0, 180)
        weather_icons = {
            "01d": "\uf00d",
            "02d": "\uf002",
            "03d": "\uf013",
            "04d": "\uf012",
            "09d": "\uf01a",
            "10d": "\uf019",
            "11d": "\uf01e",
            "13d": "\uf01b",
            "50d": "\uf014",
            "01n": "\uf02e",
            "02n": "\uf013",
            "03n": "\uf013",
            "04n": "\uf013",
            "09n": "\uf037",
            "10n": "\uf036",
            "11n": "\uf03b",
            "13n": "\uf038",
            "50n": "\uf023",
        }
        icon_code = summary.get("icon")
        glyph = weather_icons.get(icon_code, "\uf07b")
        if self.color_conditions:
            if icon_code in ("09d", "10d", "09n", "10n"):
                icon_color = (0, 80, 200)
            elif icon_code in ("11d", "11n"):
                icon_color = (120, 0, 120)
            elif icon_code in ("01d",):
                icon_color = (255, 170, 0)
            elif icon_code in ("01n",):
                icon_color = (120, 0, 180)
            elif icon_code in ("13d", "13n"):
                icon_color = (0, 120, 200)
        icon_h = icon_font.getbbox(glyph)[3] - icon_font.getbbox(glyph)[1]
        lines_center = line1_y + (line3_y - line1_y + line_gap) // 2
        icon_y = lines_center - icon_h // 2 - int(self.fontsize * 0.35) - left_raise
        draw.text((icon_x, icon_y), glyph, fill=icon_color, font=icon_font)

        text_left = x0 + 66
        temp_str = ""
        temp_y = line1_y
        temp_h = 0
        if self.show_current_temp:
            temp_val = float(summary.get("temp_now", 0))
            temp_str = f"{temp_val:.0f}°C"
            temp_h = temp_font.getbbox(temp_str)[3] - temp_font.getbbox(temp_str)[1]
            temp_y = (line1_y + line2_y) // 2 - temp_h // 2 + int(self.fontsize * 0.25) - left_raise
            draw.text((text_left, temp_y), temp_str, fill="black", font=temp_font)
            status_y = line3_y - 2
        else:
            status_y = line1_y - 2

        status_text = summary.get("status", "")
        right_x = x0 + int((x1 - x0) * 0.70)
        max_status_w = max(10, (right_x - 12) - text_left)
        words = status_text.split()
        status_main = ""
        status_second = ""
        status_font = side_value_font
        if status_text and side_value_font.getbbox(status_text)[2] <= max_status_w:
            status_main = status_text
        else:
            status_font = status_small_font
            for i in range(1, len(words) + 1):
                first = " ".join(words[:i])
                second = " ".join(words[i:])
                if status_small_font.getbbox(first)[2] <= max_status_w and status_small_font.getbbox(second)[2] <= max_status_w:
                    status_main = first
                    status_second = second
            if not status_main:
                status_main = _truncate_text(status_small_font, status_text, max_status_w)
        if status_second:
            h_small = status_small_font.getbbox("Ag")[3] - status_small_font.getbbox("Ag")[1]
            draw.text((text_left, status_y - h_small - 1), status_main, fill="black", font=status_small_font)
            draw.text((text_left, status_y), status_second, fill="black", font=status_small_font)
        else:
            draw.text((text_left, status_y), status_main, fill="black", font=status_font)

        sunmoon_col_x = x0 + int((x1 - x0) * 0.40)
        tmin = float(summary["temp_min"])
        tmax = float(summary["temp_max"])
        if round(tmin) == round(tmax) and abs(tmax - tmin) >= 0.2:
            tmin_str = f"{tmin:.1f}°C"
            tmax_str = f"{tmax:.1f}°C"
        else:
            tmin_str = f"{tmin:.0f}°C"
            tmax_str = f"{tmax:.0f}°C"

        sunrise = summary.get("sunrise")
        if sunrise:
            sunrise_local = arrow.get(sunrise).format("HH:mm")
            sunrise_note = ""
            if self.show_suntime and summary.get("sunshine_hours") is not None:
                st = _format_suntime(summary.get("sunshine_hours"), self.language)
                if st:
                    sunrise_note = f" ({st})"
            sr_icon_w = sunmoon_font.getbbox("\uf051")[2] - sunmoon_font.getbbox("\uf051")[0]
            sr_icon_x = sunmoon_col_x
            sr_time_x = sunmoon_col_x + sr_icon_w + 4
            draw.text((sr_icon_x, line1_y - 1), "\uf051", fill=sun_color, font=sunmoon_font)
            draw.text((sr_time_x, line1_y), sunrise_local, fill=sun_color, font=side_value_font)
            if sunrise_note:
                main_w = side_value_font.getbbox(sunrise_local)[2] - side_value_font.getbbox(sunrise_local)[0]
                note_y = line1_y + 5
                draw.text((sr_time_x + main_w + 3, note_y), sunrise_note, fill=sun_color, font=side_note_font)

        max_color = (0, 0, 0)
        min_color = (0, 0, 0)
        if self.color_conditions:
            max_color = (180, 0, 0)
            min_color = (0, 70, 170)
        draw.text((right_x, line1_y), "↑", fill=max_color, font=arrow_font)
        draw.text((right_x + 14, line1_y), tmax_str, fill=max_color, font=side_value_font)
        draw.text((right_x + 60, line1_y), "↓", fill=min_color, font=arrow_font)
        draw.text((right_x + 74, line1_y), tmin_str, fill=min_color, font=side_value_font)

        pop = int(round(summary.get("pop", 0)))
        precip_rate = max(0.0, float(summary.get("precip_rate_mmh", 0.0) or 0.0))
        precip_rate_str = f"{precip_rate:.1f}".rstrip("0").rstrip(".")
        if not precip_rate_str:
            precip_rate_str = "0"
        if self.language.startswith("de"):
            precip_rate_str = precip_rate_str.replace(".", ",")
        sunset = summary.get("sunset")
        if sunset:
            sunset_local = arrow.get(sunset).format("HH:mm")
            ss_icon_w = sunmoon_font.getbbox("\uf052")[2] - sunmoon_font.getbbox("\uf052")[0]
            ss_icon_x = sunmoon_col_x
            ss_time_x = sunmoon_col_x + ss_icon_w + 4
            draw.text((ss_icon_x, line2_y - 1), "\uf052", fill=sun_color, font=sunmoon_font)
            draw.text((ss_time_x, line2_y), sunset_local, fill=sun_color, font=side_value_font)

        icon_code = summary.get("icon", "")
        if icon_code.startswith("13"):
            precip_icon = "\uf01b"
        elif pop > 0:
            precip_icon = "\uf019"
        else:
            precip_icon = "\uf00c"
        precip_icon_w = sunmoon_font.getbbox(precip_icon)[2] - sunmoon_font.getbbox(precip_icon)[0]
        precip_icon_x = right_x
        precip_text_x = right_x + precip_icon_w + 6
        draw.text((precip_icon_x, line2_y - 2), precip_icon, fill=precip_color, font=sunmoon_font)
        precip_main = f"{pop}%"
        precip_note = f" ({precip_rate_str} mm/h)"
        draw.text((precip_text_x, line2_y), precip_main, fill=precip_color, font=side_value_font)
        main_w = side_value_font.getbbox(precip_main)[2] - side_value_font.getbbox(precip_main)[0]
        note_y = line2_y + 5
        draw.text((precip_text_x + main_w + 3, note_y), precip_note, fill=precip_color, font=side_note_font)

        wind_ms = summary.get("wind", 0)
        wind_kmh = wind_ms * 3.6
        moon_icon = self._moon_phase_icon(now)
        moon_name = self._moon_phase_name(now)
        moon_icon_w = sunmoon_font.getbbox(moon_icon)[2] - sunmoon_font.getbbox(moon_icon)[0]
        max_moon_w = max(0, (right_x - 6) - (sunmoon_col_x + moon_icon_w + 8))
        moon_name = _truncate_text(side_value_font, moon_name, max_moon_w)
        moon_name_x = sunmoon_col_x + moon_icon_w + 11
        moon_icon_x = sunmoon_col_x + 5
        draw.text((moon_icon_x, line3_y - 1), moon_icon, fill=moon_color, font=sunmoon_font)
        draw.text((moon_name_x, line3_y), moon_name, fill=moon_color, font=side_value_font)

        wind_icon = "\uf050"
        wind_icon_w = sunmoon_font.getbbox(wind_icon)[2] - sunmoon_font.getbbox(wind_icon)[0]
        wind_icon_x = right_x
        wind_text_x = right_x + wind_icon_w + 6
        min_w = side_value_font.getbbox(tmin_str)[2] - side_value_font.getbbox(tmin_str)[0]
        max_w = side_value_font.getbbox(tmax_str)[2] - side_value_font.getbbox(tmax_str)[0]
        precip_note_w = side_note_font.getbbox(precip_note)[2] - side_note_font.getbbox(precip_note)[0]
        wind_text = f"{wind_kmh:.0f} km/h"
        wind_text_w = side_value_font.getbbox(wind_text)[2] - side_value_font.getbbox(wind_text)[0]

        draw.text((wind_icon_x, line3_y - 2), wind_icon, fill=wind_color, font=sunmoon_font)
        draw.text((wind_text_x, line3_y), wind_text, fill=wind_color, font=side_value_font)

        # Draw warning icon(s) last so nothing can overlap them.
        warning_markers = summary.get("dwd_warning_markers") or []
        focus_counts: Dict[str, int] = {}
        generic_idx = 0
        for marker in warning_markers:
            color, focus = marker
            focused = focus in ("min_temp", "max_temp", "precip", "wind")
            warn_size = int(self.fontsize * (0.74 if focused else 1.0))
            key = str(focus or "generic")
            idx = focus_counts.get(key, 0)
            focus_counts[key] = idx + 1
            if focus == "min_temp":
                warn_x = right_x + 74 + min_w + 4 + idx * (warn_size + 2)
                warn_y = line1_y + 1
            elif focus == "max_temp":
                warn_x = right_x + 14 + max_w + 4 + idx * (warn_size + 2)
                warn_y = line1_y + 1
            elif focus == "precip":
                warn_x = precip_text_x + main_w + precip_note_w + 4 + idx * (warn_size + 2)
                warn_y = line2_y + 1
            elif focus == "wind":
                warn_x = wind_text_x + wind_text_w + 4 + idx * (warn_size + 2)
                warn_y = line3_y + 1
            elif self.show_current_temp:
                temp_w = temp_font.getbbox(temp_str)[2] - temp_font.getbbox(temp_str)[0]
                warn_x = text_left + temp_w + 12 + (generic_idx * (warn_size + 3))
                warn_y = int(temp_y + (temp_h - warn_size) / 2) + 5
                generic_idx += 1
            else:
                warn_x = text_left + 6 + (generic_idx * (warn_size + 3))
                warn_y = line1_y + 1
                generic_idx += 1
            warn_x = max(x0 + 2, min(x1 - warn_size - 2, warn_x))
            warn_y = max(y0 + 1, min(y1 - warn_size - 1, warn_y))
            _draw_alert_icon(draw, warn_x, warn_y, warn_size, color)

    def _draw_calendar(self, base: Image.Image, box: Tuple[int, int, int, int], now: arrow.Arrow, events: List[Dict]):
        x0, y0, x1, y1 = box
        draw = ImageDraw.Draw(base)

        calendar_start = self._calendar_start(now)
        total_days = self.calendar_weeks * 7
        event_map = self._build_event_map(events)

        week_col_w = int((x1 - x0) * 0.08)
        day_col_w = int((x1 - x0 - week_col_w) / 7)

        header_h = max(int(self.fontsize * 2.2), int((y1 - y0) * 0.16))
        row_h = int((y1 - y0 - header_h) / self.calendar_weeks)
        row_h = max(row_h, int(self.fontsize * 1.4))

        weekday_font = _load_font("ui_bold", int(self.fontsize * 1.2))
        day_font = _load_font("ui_bold", int(self.fontsize * 0.9))
        week_font = _load_font("regular", int(self.fontsize * 0.85))
        moon_day_font = _load_font("weather", int(self.fontsize * 0.50))
        moon_day_color = (130, 0, 180)
        weekend_fill = (220, 220, 220)

        if self.language.startswith("de"):
            weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
            if self.week_start == "Sunday":
                weekdays = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]
        else:
            weekdays = []
            for i in range(7):
                day = calendar_start.shift(days=i)
                weekdays.append(day.format("dd", locale=self.language))

        draw.rectangle((x0, y0, x1, y0 + header_h), fill="white")
        # No KW label (just week numbers on the left)
        for i, label in enumerate(weekdays):
            if i >= 5:
                draw.rectangle((x0 + week_col_w + i * day_col_w, y0, x0 + week_col_w + (i + 1) * day_col_w, y0 + header_h), fill=weekend_fill)
            w = weekday_font.getbbox(label)[2] - weekday_font.getbbox(label)[0]
            h = weekday_font.getbbox(label)[3] - weekday_font.getbbox(label)[1]
            cx = x0 + week_col_w + i * day_col_w + (day_col_w - w) // 2
            cy = y0 + max(4, (header_h - h) // 2)
            draw.text((cx, cy), label, fill="black", font=weekday_font)

        for week in range(self.calendar_weeks):
            week_start = calendar_start.shift(days=week * 7)
            row_y = y0 + header_h + week * row_h
            # Week number in left column
            week_num = str(week_start.isocalendar()[1])
            ww = week_font.getbbox(week_num)[2] - week_font.getbbox(week_num)[0]
            wx = x0 + (week_col_w - ww) // 2
            wy = row_y + int(row_h * 0.35)
            draw.text((wx, wy), week_num, fill="black", font=week_font)
            for d in range(7):
                cell_x = x0 + week_col_w + d * day_col_w
                cell_y = row_y
                cell_date = week_start.shift(days=d).date()

                if d >= 5:
                    draw.rectangle((cell_x, cell_y, cell_x + day_col_w, cell_y + row_h), fill=weekend_fill)

                if self.month_label_first_day and cell_date.day == 1:
                    day_num = arrow.get(cell_date).format("MMM", locale=self.language).upper()
                else:
                    day_num = str(cell_date.day)
                if cell_date == now.date():
                    r = int(min(day_col_w, row_h) * 0.28)
                    cx = cell_x + day_col_w // 2
                    cy = cell_y + int(row_h * 0.35)
                    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(220, 0, 0))
                    draw.text((cx, cy), day_num, fill="white", font=day_font, anchor="mm")
                else:
                    w = day_font.getbbox(day_num)[2] - day_font.getbbox(day_num)[0]
                    cx = cell_x + (day_col_w - w) // 2
                    draw.text((cx, cell_y + 4), day_num, fill="black", font=day_font)

                if self.calendar_show_moon:
                    moon_dt = arrow.get(datetime(cell_date.year, cell_date.month, cell_date.day, 12, 0))
                    moon_idx = self._moon_phase_index(moon_dt)
                    prev_idx = self._moon_phase_index(moon_dt.shift(days=-1))
                    show_moon = (moon_idx != prev_idx) and (moon_idx in (0, 2, 4, 6))
                    if show_moon:
                        moon_glyph = self._moon_phase_icon(moon_dt)
                        mw = moon_day_font.getbbox(moon_glyph)[2] - moon_day_font.getbbox(moon_glyph)[0]
                        moon_x = cell_x + day_col_w - mw - 6
                        moon_y = cell_y + 1
                        draw.text((moon_x, moon_y), moon_glyph, fill=moon_day_color, font=moon_day_font)

                day_events = event_map.get(cell_date, [])
                if day_events:
                    dot_r = 4
                    max_dots = min(4, len(day_events))
                    dot_y = cell_y + row_h - 18
                    cx = cell_x + day_col_w // 2
                    total_w = max_dots * (dot_r * 2 + 2) - 2
                    start_x = cx - total_w // 2
                    for i in range(max_dots):
                        color = day_events[i].get("feed_color", (0, 0, 0))
                        is_recurring = day_events[i].get("is_recurring", False)
                        is_all_day = self._is_all_day(day_events[i])
                        is_meals = bool(day_events[i].get("is_meals", False))
                        dx = start_x + i * (dot_r * 2 + 2)
                        if is_meals:
                            _draw_cutlery_icon(draw, dx + dot_r, dot_y + dot_r - 1, dot_r * 2 + 3, color)
                        elif is_all_day:
                            draw.rectangle((dx, dot_y, dx + dot_r * 2, dot_y + dot_r * 2), fill=color)
                        else:
                            draw.ellipse((dx, dot_y, dx + dot_r * 2, dot_y + dot_r * 2), fill=color)
                        if is_recurring and not is_meals:
                            inner_r = max(1, dot_r - 2)
                            ix = dx + dot_r - inner_r
                            iy = dot_y + dot_r - inner_r
                            if is_all_day:
                                draw.rectangle((ix, iy, ix + inner_r * 2, iy + inner_r * 2), fill="white")
                            else:
                                draw.ellipse((ix, iy, ix + inner_r * 2, iy + inner_r * 2), fill="white")

    def _draw_agenda(self, base: Image.Image, box: Tuple[int, int, int, int], now: arrow.Arrow, events: List[Dict]):
        x0, y0, x1, y1 = box
        draw = ImageDraw.Draw(base)

        title_font = _load_font("ui_bold", int(self.fontsize * 0.9))
        text_font = _load_font("regular", int(self.fontsize * 0.82))

        line_h = text_font.getbbox("Ag")[3] - text_font.getbbox("Ag")[1] + 6
        cursor_y = y0

        agenda_start_day = now.floor("day")
        agenda_end_day = now.shift(days=self.agenda_days).floor("day")
        grouped: Dict[date, List[Dict]] = {}
        for event in events:
            is_all_day_evt = self._is_all_day(event)
            is_meals_evt = bool(event.get("is_meals", False))
            # Keep all-day and meals visible for the whole day, but hide regular events once they ended.
            if (not is_all_day_evt) and (not is_meals_evt) and event["end"] <= now:
                continue
            begin_day = event["begin"].floor("day")
            end_day = event["end"].floor("day")
            if is_all_day_evt:
                end_day = end_day.shift(days=-1)
            if end_day < agenda_start_day or begin_day > agenda_end_day:
                continue
            cursor = begin_day if begin_day >= agenda_start_day else agenda_start_day
            while cursor <= end_day and cursor <= agenda_end_day:
                grouped.setdefault(cursor.date(), []).append(event)
                cursor = cursor.shift(days=1)

        if not grouped:
            draw.text((x0, cursor_y), "Keine Termine" if self.language == "de" else "No events", fill="black", font=title_font)
            return

        for day in sorted(grouped.keys()):
            day_events = grouped.get(day, [])
            if not day_events:
                continue
            if cursor_y + (line_h * 2) > y1:
                break
            day_arrow = arrow.get(day)
            delta_days = (day - now.date()).days
            if delta_days < max(0, self.agenda_relative_days):
                if delta_days == 0:
                    header = "Heute" if self.language == "de" else "Today"
                elif delta_days == 1:
                    header = "Morgen" if self.language == "de" else "Tomorrow"
                elif delta_days == 2:
                    header = "Übermorgen" if self.language == "de" else "Day after tomorrow"
                else:
                    header = _capitalize_first(day_arrow.format("dddd", locale=self.language))
            elif day <= now.shift(days=6).date():
                header = _capitalize_first(day_arrow.format(self.agenda_weekday_format, locale=self.language))
            else:
                header = day_arrow.format(self.agenda_date_format, locale=self.language)
            draw.text((x0, cursor_y), header, fill="black", font=title_font)
            cursor_y += line_h

            day_events.sort(key=lambda e: (0 if self._is_all_day(e) else 1, e["begin"]))
            for event in day_events:
                if cursor_y + line_h > y1:
                    break
                is_all_day = self._is_all_day(event)
                is_meals = bool(event.get("is_meals", False))
                if is_all_day:
                    time_str = "Ganztägig" if self.language.startswith("de") else "All day"
                elif is_meals:
                    time_str = _meal_label(event["begin"].hour, self.language)
                else:
                    time_str = event["begin"].format(self.time_format, locale=self.language)
                title = _strip_emoji(event.get("title", ""))

                dot_r = 4
                dot_x = x0 + 2
                dot_y = cursor_y + (line_h - dot_r * 2) // 2 + 1
                color = event.get("feed_color", (0, 0, 0))
                if is_meals:
                    _draw_cutlery_icon(draw, dot_x + dot_r, dot_y + dot_r - 1, dot_r * 2 + 3, color)
                elif is_all_day:
                    draw.rectangle((dot_x, dot_y, dot_x + dot_r * 2, dot_y + dot_r * 2), fill=color)
                else:
                    draw.ellipse((dot_x, dot_y, dot_x + dot_r * 2, dot_y + dot_r * 2), fill=color)
                if event.get("is_recurring", False) and not is_meals:
                    inner_r = max(1, dot_r - 2)
                    ix = dot_x + dot_r - inner_r
                    iy = dot_y + dot_r - inner_r
                    if is_all_day:
                        draw.rectangle((ix, iy, ix + inner_r * 2, iy + inner_r * 2), fill="white")
                    else:
                        draw.ellipse((ix, iy, ix + inner_r * 2, iy + inner_r * 2), fill="white")

                text_x = dot_x + dot_r * 2 + 6
                max_width = x1 - text_x - 4

                def wrap_line(text: str, max_w: int) -> List[str]:
                    words = text.split(" ")
                    lines = []
                    current = ""
                    for word in words:
                        test = (current + " " + word).strip()
                        if text_font.getbbox(test)[2] <= max_w:
                            current = test
                        else:
                            if current:
                                lines.append(current)
                            current = word
                    if current:
                        lines.append(current)
                    return lines

                if time_str:
                    line_text = f"{time_str}: {title}" if (is_all_day or is_meals) else f"{time_str} - {title}"
                else:
                    line_text = title
                wrapped = wrap_line(line_text, max_width)[:2]
                for wline in wrapped:
                    if cursor_y + line_h > y1:
                        break
                    draw.text((text_x, cursor_y), wline, fill="black", font=text_font)
                    cursor_y += line_h

    def generate_image(self) -> Image.Image:
        im_width = int(self.width - (self.padding_left + self.padding_right))
        im_height = int(self.height - (self.padding_top + self.padding_bottom))
        base_inner = Image.new("RGB", (im_width, im_height), "white")
        draw = ImageDraw.Draw(base_inner)

        now = arrow.now(tz=self.tzinfo)
        calendar_start = self._calendar_start(now)
        calendar_end = calendar_start.shift(days=self.calendar_weeks * 7)
        events = self._load_events(calendar_start.shift(days=-1), calendar_end)

        header_h = int(im_height * 0.06)
        weather_h = int(im_height * 0.30)
        calendar_h = int(im_height * 0.39)
        agenda_h = im_height - header_h - weather_h - calendar_h - 4

        y = 0
        header_box = (0, y, im_width, y + header_h)
        y += header_h + 1
        weather_box = (0, y, im_width, y + weather_h)
        y += weather_h + 2
        calendar_box = (0, y, im_width, y + calendar_h)
        y += calendar_h + 2
        agenda_box = (0, y, im_width, y + agenda_h)

        self._draw_header(draw, header_box, now)
        self._draw_weather(base_inner, weather_box)
        self._draw_calendar(base_inner, calendar_box, now, events)
        self._draw_agenda(base_inner, agenda_box, now, events)

        # Footer status (bottom-right)
        percent = self.battery_conf.get("percent")
        updated = self.battery_conf.get("updated")
        if updated is None:
            updated = now.format("DD.MM.YYYY HH:mm")
        # Footer icons from weather font for better e-ink visibility
        footer_font = _load_font("regular", int(self.fontsize * 0.6))
        footer_icon_font = _load_font("weather", int(self.fontsize * 0.75))
        base_draw = ImageDraw.Draw(base_inner)

        show_battery = percent is not None and percent <= self.battery_show_below
        text = None
        if self.show_last_updated:
            try:
                updated_dt = arrow.get(updated, "DD.MM.YYYY HH:mm")
            except Exception:
                try:
                    updated_dt = arrow.get(updated)
                except Exception:
                    updated_dt = now
            text = updated_dt.format(self.last_updated_format, locale=self.language)
        if self.show_weather_fallback_info and self.weather_fallback_note:
            if text:
                text = f"{text} | {self.weather_fallback_note}"
            else:
                text = self.weather_fallback_note
        icon_refresh = "\uf04c"  # wi-refresh

        batt_w = 38
        batt_h = 16
        batt_pad = 6
        # Measure text widths
        w_text = 0
        w_icon_refresh = footer_icon_font.getbbox(icon_refresh)[2] - footer_icon_font.getbbox(icon_refresh)[0]
        if text:
            max_text_w = im_width - 12 - (batt_w + batt_pad if show_battery else 0) - (w_icon_refresh + 4)
            text = _truncate_text(footer_font, text, max(40, max_text_w))
            w_text = footer_font.getbbox(text)[2] - footer_font.getbbox(text)[0]
        total_w = (w_icon_refresh + 4 + w_text if text else 0) + (batt_w + (batt_pad if text else 0) if show_battery else 0)
        if total_w == 0:
            return base_inner
        x = im_width - total_w - 4
        y = im_height - int(self.fontsize * 0.9) - 1

        if text:
            base_draw.text((x, y - 3), icon_refresh, fill="black", font=footer_icon_font)
            x += w_icon_refresh + 4
            base_draw.text((x, y), text, fill="black", font=footer_font)

        if show_battery:
            if text:
                x += w_text + batt_pad
            # Battery outline
            bx = x
            by = y
            base_draw.rectangle((bx, by, bx + batt_w, by + batt_h), outline="black", width=1)
            # Battery terminal
            base_draw.rectangle((bx + batt_w + 1, by + 4, bx + batt_w + 3, by + batt_h - 4), outline="black", width=1)

            # Fill level
            lvl = max(0, min(100, int(percent)))
            fill_w = int((batt_w - 4) * (lvl / 100.0))
            if fill_w > 0:
                base_draw.rectangle((bx + 2, by + 2, bx + 2 + fill_w, by + batt_h - 2), fill="black")

            # Percent text inside
            pct_text = f"{lvl}%"
            pct_font = _load_font("regular", int(self.fontsize * 0.5))
            pw = pct_font.getbbox(pct_text)[2] - pct_font.getbbox(pct_text)[0]
            ph = pct_font.getbbox(pct_text)[3] - pct_font.getbbox(pct_text)[1]
            tx = bx + (batt_w - pw) // 2
            ty = by + (batt_h - ph) // 2 - 2
            base_draw.text((tx, ty), pct_text, fill="white" if lvl > 40 else "black", font=pct_font)
        # Return inner image; padding will be applied after rotation
        return base_inner
