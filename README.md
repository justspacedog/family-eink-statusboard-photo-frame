# Family E-Ink Statusboard and Photo Frame
![Statusboard Frame](statusboard-frame.jpg)

An e-ink family statusboard for a 7.3" Spectra 6 panel.

I originally tested an immmich photo frame (EPF e-paper frame (https://github.com/jwchen119/EPF)), but my wife did not like photos on this e-ink display, so this project focuses on a practical statusboard view (weather, calendar, agenda) while keeping the immmich function as a backup.

## What It Does
- Turns a 7.3" Spectra 6 e-ink frame into a family dashboard with two modes:
  - `statusboard` mode: weather + calendar + agenda + device info
  - `immich` mode: photo frame fallback

- In `statusboard` mode, it shows:
  - localized date/header
  - current weather summary (icon, condition, temperature)
  - sunrise/sunset time, moon phase, wind, precipitation probability and amount
  - multi-day weather chart (temperature + precipitation, with day/night shading)
  - monthly calendar with event markers
    - `●` round: event
    - `○` round outline: recurring event
    - `■` square: all-day event
    - `□` square outline: recurring all-day event
    - `pot icon`: meal event
  - agenda list with relative day labels (e.g. Today/Tomorrow) and configurable formatting
  - footer info like last refresh and battery state

- Supports two weather providers:
  - OpenWeatherMap (OWM)
  - Deutscher Wetterdienst (DWD) via BrightSky API (note that unrise/sunset times are calculared local in this mode (as DWD does not offer those) with the [National Oceanic and Atmospheric Administration (NOAA) solar algorithm](https://gml.noaa.gov/grad/solcalc/calcdetails.html))

- Supports DWD warning markers:
  - places warning icons next to affected values (min/max/precip/wind)
  - falls back to current-temperature area when no specific target can be classified

- Lets you configure weather summary scopes per metric:
  - current day (default, similar to BrightSky demo)
  - or next 24 hours

- Exposes HTTP endpoints for frame integration and Home Assistant:
  - `/download`, `/battery`, `/mode`, `/sleep`, `/setting`
  
## How It Works

1. Flask serves a web settings UI and API endpoints.
2. The frame requests `/download` and receives generated C-style pixel output (`image.c`).
3. Weather + calendar data are fetched server-side.
4. Rendering is done with Pillow + Matplotlib.
5. Configuration is persisted in `/config/config.yaml`.

## Bill of Materials (BOM)

| Qty | Part | Notes |
|---|---|---|
| 1x | [FireBeetle 2 ESP32-C6](https://www.dfrobot.com/product-2771.html) | no affiliate |
| 1x | [7.3-inch E Ink Spectra 6 (E6) Full Color E-Paper Display Module + HAT](https://www.waveshare.com/7.3inch-e-paper-hat-e.htm) | no affiliate |
| 1x | 3.7V LiPo battery, e.g. [EREMIT 4,000 mAh](https://www.eremit.de/p/eremit-3-7v-4-000mah-high-cap?cmdf=EREMIT+3.7V+4.000mAh+High+Cap.) | 4,000 mAh gives more capacity; does **not** fit the default CAD battery pocket |
| 1x | Button, e.g. [AliExpress example](https://a.aliexpress.com/_mqVMyRf) | this one is too large for the default print hole; hole had to be widened |
| 2x | Cables for button (e.g. Dupont) | short jumper wires |
| 2-4x | M3 screws, 8 mm (or shorter) | for HAT to 3D print |
| 4x | M4 screws, 14 mm (or shorter) | for upper panel to bottom panel |
| 1x set | 3D prints from /CAD | print `display_card.STEP` and `bottom_panel.STEP` in white for passepartout effect |
| 1x | 6x8 inch frame (8x10 outer), e.g. [BGA Store](https://www.bgastore.de/rahmen-edsbyn-acrylglas-eiche-8x10-inches-20-32x25-4-cm) | 6x8 is less common in Germany |
| 1x | Soldering iron + glue | assembly |
| optional | USB-C magnetic cable | easier charging/access |

## Wiring (Top to Bottom on HAT)

> Check cable colors before soldering. Different cable sets may use different colors.

| HAT | Cable color | Pin |
|---|---|---|
| VCC | gray | 3V3 |
| GND | brown | GND |
| SCLK | yellow | 23 |
| DIN | blue | 22 |
| BUSY | violet | 18 |
| CS | orange | 1 |
| RST | white | 14 |
| DC | green | 8 |
| BTN | XX | other side, after installing button on 3D: GND |
| BTN | XX | other side, after installing button on 3D: 2 |

## Docker Compose

```yaml
services:
  frame-server:
    image: python:3.9-slim
    container_name: frame-server
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - TZ=Europe/Berlin
      - OWM_API_KEY=your_openweathermap_key
      - IMMICH_API_KEY=your_immich_api_key
    volumes:
      - ./frame-server/app:/app
      - ./frame-server/config.yaml:/config/config.yaml # not really needed to link outside, as it is generated with the settings.html
```

## Home Assistant Integration

### REST sensors

```yaml
sensor:
  - platform: rest
    name: epf_battery
    resource: http://YOUR_HOST:5000/battery
    value_template: "{{ value_json.percent | float(0) }}"
    unit_of_measurement: "%"
    scan_interval: 300

  - platform: rest
    name: epf_mode
    resource: http://YOUR_HOST:5000/mode
    value_template: "{{ value_json.mode }}"
    scan_interval: 30

  - platform: rest
    name: epf_next_update
    resource: http://YOUR_HOST:5000/sleep
    value_template: "{{ value_json.next_wakeup }}"
    scan_interval: 60
```

### Mode switch (`statusboard` / `immich`)

```yaml
rest_command:
  epf_set_mode:
    url: "http://YOUR_HOST:5000/mode"
    method: POST
    content_type: "application/json"
    payload: '{"mode":"{{ mode }}"}'

input_select:
  epf_display_mode:
    name: EPF Display Mode
    options:
      - statusboard
      - immich

automation:
  - alias: EPF Mode Sync
    trigger:
      - platform: state
        entity_id: input_select.epf_display_mode
    action:
      - service: rest_command.epf_set_mode
        data:
          mode: "{{ states('input_select.epf_display_mode') }}"
```

## Acknowledgements

Original Project:
- EPF Project (hardware/CAD/Arduino basis): [jwchen119/EPF](https://github.com/jwchen119/EPF)

Inspiration for Layout:
- MagInkCal: [speedyg0nz/MagInkCal](https://github.com/speedyg0nz/MagInkCal)
- Inkycal: [aceisace/Inkycal](https://github.com/aceisace/Inkycal)

Resources:
- Bright Sky (DWD API wrapper): [jdemaeyer/brightsky](https://github.com/jdemaeyer/brightsky) and [API docs](https://brightsky.dev/docs/)
- Weather icons/font: [erikflowers/weather-icons](https://github.com/erikflowers/weather-icons)
- Weather providers: [DWD](https://www.dwd.de/) and [OpenWeatherMap](https://openweathermap.org/)

## Built With Codex

This statusboard was vibe-coded with Codex (GPT-5).
