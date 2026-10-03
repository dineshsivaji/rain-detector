#!/usr/bin/env python3

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import subprocess
from zoneinfo import ZoneInfo


# =============================================================================
# CONFIGURATION
# =============================================================================

WHATSAPP_GROUP = "GRP-ID@g.us"
NATS_SUBJECT = "notify.whatsapp"

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

LOCATIONS_FILE = Path("locations.json")
OUTPUT_FILE = Path("weather_status.json")
SUMMARY_STATE_FILE = Path("weather_summary_last_sent.json")

# Check every hour
CHECK_INTERVAL_SECONDS = 60 * 60


# =============================================================================
# OPEN-METEO VARIABLES
# =============================================================================

HOURLY_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "rain",
    "showers",
    "weather_code",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
]


# =============================================================================
# WMO WEATHER CODES
# =============================================================================

WEATHER_CODE_DESCRIPTIONS = {
    0: "Clear sky",

    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",

    45: "Fog",
    48: "Depositing rime fog",

    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",

    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",

    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",

    66: "Light freezing rain",
    67: "Heavy freezing rain",

    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",

    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",

    85: "Slight snow showers",
    86: "Heavy snow showers",

    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


# =============================================================================
# RAIN DISPLAY
# =============================================================================

RAIN_DISPLAY = {
    "THUNDERSTORM": (
        "🔴",
        "Thunderstorm",
    ),

    "HEAVY_RAIN": (
        "🔴",
        "Heavy rain",
    ),

    "MODERATE_RAIN": (
        "🟠",
        "Moderate rain",
    ),

    "LIGHT_RAIN": (
        "🟡",
        "Light rain",
    ),

    "RAIN_LIKELY": (
        "🟡",
        "Rain likely",
    ),

    "RAIN_POSSIBLE": (
        "🟡",
        "Rain possible",
    ),

    "NO_SIGNIFICANT_RAIN": (
        "🟢",
        "No significant rain",
    ),
}


# =============================================================================
# GENERIC HELPERS
# =============================================================================

def safe_float(value):
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def atomic_write_json(path, data):
    """
    Write JSON atomically.

    This prevents a partially written JSON file if the process
    is interrupted while writing.
    """

    tmp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with tmp_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    tmp_path.replace(path)


def get_array_value(array, index):
    """
    Safely retrieve array[index].

    Open-Meteo normally returns arrays with identical lengths,
    but this prevents an unexpected malformed response from
    crashing the entire processing loop.
    """

    if not isinstance(array, list):
        return None

    if index < 0 or index >= len(array):
        return None

    return array[index]


# =============================================================================
# LOAD LOCATIONS
# =============================================================================

def load_locations():
    """
    Load locations.json.

    Expected format:

    {
        "locations": [
            {
                "name": "City Park Hotel Bangkok Pratunam",
                "short_name": "city_park",
                "type": "attraction",
                "lat": 13.7568,
                "lon": 100.5345
            }
        ]
    }
    """

    if not LOCATIONS_FILE.exists():
        raise FileNotFoundError(
            f"Locations file not found: "
            f"{LOCATIONS_FILE}"
        )

    with LOCATIONS_FILE.open(
        "r",
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    # -------------------------------------------------------------------------
    # Your actual locations.json is an object:
    #
    # {
    #     "locations": [...]
    # }
    # -------------------------------------------------------------------------

    if not isinstance(data, dict):
        raise ValueError(
            f"{LOCATIONS_FILE} must contain "
            f"a JSON object"
        )

    locations = data.get("locations")

    if not isinstance(locations, list):
        raise ValueError(
            f"{LOCATIONS_FILE} must contain "
            f"a 'locations' JSON list"
        )

    if not locations:
        raise ValueError(
            f"{LOCATIONS_FILE} contains no locations"
        )

    # Validate every location.
    for index, location in enumerate(locations):

        if not isinstance(location, dict):
            raise ValueError(
                f"Location #{index} is not "
                f"a JSON object"
            )

        required_fields = (
            "name",
            "short_name",
            "lat",
            "lon",
        )

        for field in required_fields:
            if field not in location:
                raise ValueError(
                    f"Location #{index} "
                    f"('{location.get('name', '?')}') "
                    f"missing '{field}'"
                )

        try:
            float(location["lat"])
            float(location["lon"])
        except (TypeError, ValueError):
            raise ValueError(
                f"Location #{index} "
                f"('{location['name']}') has invalid "
                f"lat/lon"
            )

    return locations


# =============================================================================
# FETCH OPEN-METEO
# =============================================================================

def fetch_weather(locations):
    """
    Fetch weather for all configured locations.

    Open-Meteo behavior:

    Multiple coordinates:
        [
            {
                "latitude": ...,
                "longitude": ...,
                "hourly": {
                    "time": [...],
                    ...
                }
            },
            ...
        ]

    Single coordinate:
        {
            "latitude": ...,
            "longitude": ...,
            "hourly": {
                ...
            }
        }

    This function always normalizes the result to:

        list[dict]
    """

    # -------------------------------------------------------------------------
    # Your locations.json uses "lat" and "lon".
    # -------------------------------------------------------------------------

    latitudes = ",".join(
        str(location["lat"])
        for location in locations
    )

    longitudes = ",".join(
        str(location["lon"])
        for location in locations
    )

    params = {
        "latitude": latitudes,
        "longitude": longitudes,

        "hourly": ",".join(
            HOURLY_VARIABLES
        ),

        "forecast_days": 7,

        "timezone": "Asia/Bangkok",

        "temperature_unit": "celsius",

        "wind_speed_unit": "kmh",

        "precipitation_unit": "mm",
    }

    url = (
        OPEN_METEO_URL
        + "?"
        + urlencode(params)
    )

    print(
        f"Open-Meteo request for "
        f"{len(locations)} locations..."
    )

    request = Request(
        url,
        headers={
            "User-Agent":
                "ThailandWeatherMonitor/1.0",
            "Accept":
                "application/json",
        },
    )

    with urlopen(
        request,
        timeout=30,
    ) as response:

        raw = response.read()

    data = json.loads(raw)

    # -------------------------------------------------------------------------
    # Normalize top-level response.
    # -------------------------------------------------------------------------

    if isinstance(data, dict):

        # Single-coordinate response.
        data = [data]

    elif isinstance(data, list):

        # Multiple-coordinate response.
        pass

    else:
        raise ValueError(
            "Unexpected Open-Meteo response type: "
            f"{type(data).__name__}"
        )

    # -------------------------------------------------------------------------
    # Verify response count.
    # -------------------------------------------------------------------------

    if len(data) != len(locations):
        raise ValueError(
            "Open-Meteo returned "
            f"{len(data)} location responses, "
            f"but {len(locations)} locations "
            f"were requested"
        )

    # -------------------------------------------------------------------------
    # Validate each response.
    # -------------------------------------------------------------------------

    for index, item in enumerate(data):

        if not isinstance(item, dict):
            raise ValueError(
                f"Open-Meteo response #{index} "
                f"is {type(item).__name__}, "
                f"expected dict"
            )

        hourly = item.get("hourly")

        if not isinstance(hourly, dict):
            raise ValueError(
                f"Open-Meteo response #{index} "
                f"has invalid 'hourly' type: "
                f"{type(hourly).__name__}"
            )

        times = hourly.get("time")

        if not isinstance(times, list):
            raise ValueError(
                f"Open-Meteo response #{index} "
                f"has invalid 'hourly.time' type: "
                f"{type(times).__name__}"
            )

        if not times:
            raise ValueError(
                f"Open-Meteo response #{index} "
                f"has an empty hourly.time array"
            )

    print(
        f"Received {len(data)} Open-Meteo responses."
    )

    return data


# =============================================================================
# TIME HANDLING
# =============================================================================

def parse_open_meteo_time(value):
    """
    Convert an Open-Meteo local timestamp into
    an aware datetime in Asia/Bangkok.

    Example:

        2026-10-03T20:00
    """

    if not isinstance(value, str):
        return None

    try:
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=BANGKOK_TZ
            )

        return dt

    except ValueError:
        return None


def find_current_hour(hourly):
    """
    Find the Open-Meteo forecast hour closest to now.

    hourly MUST be a dictionary:

        {
            "time": [...],
            "temperature_2m": [...],
            ...
        }
    """

    if not isinstance(hourly, dict):
        raise TypeError(
            "find_current_hour() expected "
            f"hourly dict, got "
            f"{type(hourly).__name__}"
        )

    times = hourly.get("time")

    if not isinstance(times, list):
        raise TypeError(
            "hourly['time'] expected list, "
            f"got {type(times).__name__}"
        )

    if not times:
        raise ValueError(
            "Open-Meteo hourly time array "
            "is empty"
        )

    now = datetime.now(
        BANGKOK_TZ
    )

    best_index = None
    best_difference = None

    for index, value in enumerate(times):

        dt = parse_open_meteo_time(
            value
        )

        if dt is None:
            continue

        difference = abs(
            (
                dt - now
            ).total_seconds()
        )

        if (
            best_difference is None
            or difference < best_difference
        ):
            best_difference = difference
            best_index = index

    if best_index is None:
        raise ValueError(
            "Unable to parse any "
            "Open-Meteo hourly timestamps"
        )

    return best_index


# =============================================================================
# RAIN CLASSIFICATION
# =============================================================================

def classify_rain(
    weather_code,
    precipitation_probability,
    precipitation,
    rain,
    showers,
):
    """
    Determine the compact rain status.

    Priority:

        Thunderstorm
        Heavy rain
        Moderate rain
        Light rain
        Rain likely
        Rain possible
        No significant rain
    """

    code = (
        weather_code
        if weather_code is not None
        else -1
    )

    precip_probability = (
        precipitation_probability
        if precipitation_probability is not None
        else 0
    )

    precipitation = (
        precipitation
        if precipitation is not None
        else 0
    )

    rain = (
        rain
        if rain is not None
        else 0
    )

    showers = (
        showers
        if showers is not None
        else 0
    )

    # -------------------------------------------------------------------------
    # Thunderstorm
    # -------------------------------------------------------------------------

    if code in (
        95,
        96,
        99,
    ):
        return "THUNDERSTORM"

    # -------------------------------------------------------------------------
    # Actual precipitation
    # -------------------------------------------------------------------------

    actual_precipitation = max(
        precipitation,
        rain,
        showers,
    )

    if actual_precipitation >= 10:
        return "HEAVY_RAIN"

    if actual_precipitation >= 5:
        return "MODERATE_RAIN"

    if actual_precipitation >= 1:
        return "LIGHT_RAIN"

    # -------------------------------------------------------------------------
    # Probability
    # -------------------------------------------------------------------------

    if precip_probability >= 70:
        return "RAIN_LIKELY"

    if precip_probability >= 40:
        return "RAIN_POSSIBLE"

    return "NO_SIGNIFICANT_RAIN"


# =============================================================================
# PROCESS LOCATION
# =============================================================================

def process_location(
    location,
    api_response,
):
    """
    Combine our location metadata with
    the corresponding Open-Meteo response.

    IMPORTANT:

        result["hourly"]

    remains a dictionary.

    It is NOT converted to a list.
    """

    if not isinstance(api_response, dict):
        raise TypeError(
            f"api_response must be dict, "
            f"got {type(api_response).__name__}"
        )

    hourly = api_response.get(
        "hourly"
    )

    if not isinstance(hourly, dict):
        raise TypeError(
            f"{location['name']}: "
            f"'hourly' must be dict, "
            f"got {type(hourly).__name__}"
        )

    return {
        "name": location["name"],

        "short_name": location[
            "short_name"
        ],

        "type": location.get(
            "type"
        ),

        "purpose": location.get(
            "purpose"
        ),

        # Preserve our original schema.
        "latitude": float(
            location["lat"]
        ),

        "longitude": float(
            location["lon"]
        ),

        # Keep full Open-Meteo hourly data.
        "hourly": hourly,
    }


# =============================================================================
# CURRENT WEATHER SUMMARY
# =============================================================================

def create_current_summary(
    processed,
):
    """
    Create a compact current-weather
    summary for every location.
    """

    summary = []

    for location in processed:

        hourly = location[
            "hourly"
        ]

        index = find_current_hour(
            hourly
        )

        # ---------------------------------------------------------------------
        # Current forecast time
        # ---------------------------------------------------------------------

        forecast_time = get_array_value(
            hourly.get("time"),
            index,
        )

        # ---------------------------------------------------------------------
        # Weather variables
        # ---------------------------------------------------------------------

        temperature = safe_float(
            get_array_value(
                hourly.get(
                    "temperature_2m"
                ),
                index,
            )
        )

        relative_humidity = safe_float(
            get_array_value(
                hourly.get(
                    "relative_humidity_2m"
                ),
                index,
            )
        )

        apparent_temperature = safe_float(
            get_array_value(
                hourly.get(
                    "apparent_temperature"
                ),
                index,
            )
        )

        precipitation_probability = safe_float(
            get_array_value(
                hourly.get(
                    "precipitation_probability"
                ),
                index,
            )
        )

        precipitation = safe_float(
            get_array_value(
                hourly.get(
                    "precipitation"
                ),
                index,
            )
        )

        rain = safe_float(
            get_array_value(
                hourly.get(
                    "rain"
                ),
                index,
            )
        )

        showers = safe_float(
            get_array_value(
                hourly.get(
                    "showers"
                ),
                index,
            )
        )

        weather_code = safe_int(
            get_array_value(
                hourly.get(
                    "weather_code"
                ),
                index,
            )
        )

        cloud_cover = safe_float(
            get_array_value(
                hourly.get(
                    "cloud_cover"
                ),
                index,
            )
        )

        wind_speed = safe_float(
            get_array_value(
                hourly.get(
                    "wind_speed_10m"
                ),
                index,
            )
        )

        wind_direction = safe_float(
            get_array_value(
                hourly.get(
                    "wind_direction_10m"
                ),
                index,
            )
        )

        wind_gusts = safe_float(
            get_array_value(
                hourly.get(
                    "wind_gusts_10m"
                ),
                index,
            )
        )

        # ---------------------------------------------------------------------
        # Weather description
        # ---------------------------------------------------------------------

        weather_description = (
            WEATHER_CODE_DESCRIPTIONS.get(
                weather_code,
                "Unknown",
            )
        )

        # ---------------------------------------------------------------------
        # Rain status
        # ---------------------------------------------------------------------

        rain_status = classify_rain(
            weather_code=weather_code,

            precipitation_probability=(
                precipitation_probability
            ),

            precipitation=precipitation,

            rain=rain,

            showers=showers,
        )

        emoji, rain_label = (
            RAIN_DISPLAY[
                rain_status
            ]
        )

        # ---------------------------------------------------------------------
        # Add summary
        # ---------------------------------------------------------------------

        summary.append(
            {
                "name": location[
                    "name"
                ],

                "short_name": location[
                    "short_name"
                ],

                "type": location.get(
                    "type"
                ),

                "purpose": location.get(
                    "purpose"
                ),

                "latitude": location[
                    "latitude"
                ],

                "longitude": location[
                    "longitude"
                ],

                "forecast_time": (
                    forecast_time
                ),

                "rain_status": (
                    rain_status
                ),

                "emoji": emoji,

                "rain_label": (
                    rain_label
                ),

                "temperature_c": (
                    temperature
                ),

                "relative_humidity_percent": (
                    relative_humidity
                ),

                "apparent_temperature_c": (
                    apparent_temperature
                ),

                "precipitation_probability_percent": (
                    precipitation_probability
                ),

                "precipitation_mm": (
                    precipitation
                ),

                "rain_mm": rain,

                "showers_mm": showers,

                "weather_code": (
                    weather_code
                ),

                "weather": (
                    weather_description
                ),

                "cloud_cover_percent": (
                    cloud_cover
                ),

                "wind_speed_kmh": (
                    wind_speed
                ),

                "wind_direction_degrees": (
                    wind_direction
                ),

                "wind_gusts_kmh": (
                    wind_gusts
                ),
            }
        )

    return summary


# =============================================================================
# CHANGE-DETECTION SUMMARY
# =============================================================================

def create_comparison_summary(
    current_summary,
):
    """
    Build the state used to decide whether
    a WhatsApp notification is necessary.

    Deliberately excludes:

        temperature
        forecast_time
        humidity
        wind
        cloud cover

    because those values can change during
    normal forecast updates without representing
    a meaningful rain-status change.
    """

    comparison = []

    for item in current_summary:

        comparison.append(
            {
                "short_name": item[
                    "short_name"
                ],

                "rain_status": item[
                    "rain_status"
                ],

                "weather_code": item[
                    "weather_code"
                ],

                "precipitation_probability_percent": (
                    item[
                        "precipitation_probability_percent"
                    ]
                ),

                "precipitation_mm": item[
                    "precipitation_mm"
                ],

                "rain_mm": item[
                    "rain_mm"
                ],

                "showers_mm": item[
                    "showers_mm"
                ],
            }
        )

    return comparison


# =============================================================================
# LAST-SENT STATE
# =============================================================================

def load_last_sent_summary():
    """
    Load the summary that was last successfully
    sent to WhatsApp.

    Returns None on first run.
    """

    if not SUMMARY_STATE_FILE.exists():
        return None

    try:

        with SUMMARY_STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if not isinstance(data, list):
            print(
                "WARNING: summary state file "
                "does not contain a list."
            )

            print(
                "Treating this as first run."
            )

            return None

        return data

    except Exception as exc:

        print(
            "WARNING: unable to read "
            "summary state:"
        )

        print(
            f"  {exc}"
        )

        print(
            "Treating this as first run."
        )

        return None


def save_last_sent_summary(
    summary,
):
    """
    Save only after WhatsApp successfully
    publishes the notification.
    """

    atomic_write_json(
        SUMMARY_STATE_FILE,
        summary,
    )


# =============================================================================
# WHATSAPP MESSAGE
# =============================================================================

def format_whatsapp_message(
    current_summary,
):
    """
    Generate compact WhatsApp message.
    """

    now = datetime.now(
        BANGKOK_TZ
    )

    lines = [
        "🌦️ Thailand Weather Update",
        "",
        now.strftime(
            "%d %b %Y %H:%M Bangkok"
        ),
        "",
    ]

    for item in current_summary:

        lines.append(
            f"{item['emoji']} "
            f"{item['short_name']} — "
            f"{item['rain_label']}"
        )

    return "\n".join(
        lines
    )


# =============================================================================
# SEND WHATSAPP VIA NATS
# =============================================================================

def send_whatsapp(
    message,
):
    """
    Publish WhatsApp notification
    through NATS.
    """

    payload = {
        "to": WHATSAPP_GROUP,
        "text": message,
    }

    print()
    print(
        "Publishing WhatsApp notification "
        "to NATS..."
    )

    try:

        result = subprocess.run(
            [
                "nats",
                "pub",
                NATS_SUBJECT,
                json.dumps(
                    payload
                ),
            ],

            capture_output=True,

            text=True,

            timeout=15,
        )

        if result.returncode != 0:

            print(
                "ERROR: NATS publish failed"
            )

            if result.stdout:
                print(
                    "stdout:"
                )

                print(
                    result.stdout.strip()
                )

            if result.stderr:
                print(
                    "stderr:"
                )

                print(
                    result.stderr.strip()
                )

            return False

        print(
            "WhatsApp notification "
            "published successfully."
        )

        return True

    except subprocess.TimeoutExpired:

        print(
            "ERROR: NATS publish timed out."
        )

        return False

    except FileNotFoundError:

        print(
            "ERROR: 'nats' command not found."
        )

        return False

    except Exception as exc:

        print(
            "ERROR sending WhatsApp:"
        )

        print(
            f"  {exc}"
        )

        return False


# =============================================================================
# PRINT CURRENT STATUS
# =============================================================================

def print_current_status(
    current_summary,
):
    print()

    print(
        "Current weather status:"
    )

    print(
        "-" * 100
    )

    for item in current_summary:

        temperature = item[
            "temperature_c"
        ]

        if temperature is None:
            temperature_text = "?"
        else:
            temperature_text = (
                f"{temperature:.1f}°C"
            )

        probability = item[
            "precipitation_probability_percent"
        ]

        if probability is None:
            probability_text = "?"
        else:
            probability_text = (
                f"{probability:.0f}%"
            )

        print(
            f"{item['emoji']} "
            f"{item['short_name']:<25} "
            f"{item['rain_label']:<20} "
            f"{temperature_text:<8} "
            f"rain={probability_text}"
        )

    print(
        "-" * 100
    )

    print()


# =============================================================================
# WEATHER CHECK
# =============================================================================

def check_weather():

    print()
    print(
        "=" * 80
    )

    now = datetime.now(
        BANGKOK_TZ
    )

    print(
        "Weather check: "
        + now.strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    )

    print(
        "=" * 80
    )

    # -------------------------------------------------------------------------
    # Load locations
    # -------------------------------------------------------------------------

    locations = load_locations()

    print(
        f"Fetching weather for "
        f"{len(locations)} locations..."
    )

    # -------------------------------------------------------------------------
    # Fetch Open-Meteo
    # -------------------------------------------------------------------------

    api_responses = fetch_weather(
        locations
    )

    # -------------------------------------------------------------------------
    # Combine location metadata with
    # corresponding Open-Meteo response.
    # -------------------------------------------------------------------------

    processed = []

    for index, location in enumerate(
        locations
    ):

        api_response = (
            api_responses[index]
        )

        processed_location = (
            process_location(
                location,
                api_response,
            )
        )

        processed.append(
            processed_location
        )

    # -------------------------------------------------------------------------
    # Build current summary
    # -------------------------------------------------------------------------

    current_summary = (
        create_current_summary(
            processed
        )
    )

    # -------------------------------------------------------------------------
    # Save complete weather data.
    #
    # weather_status.json contains:
    #
    #   - timestamp
    #   - timezone
    #   - all locations
    #   - all hourly forecast data
    #   - current summary
    # -------------------------------------------------------------------------

    weather_output = {
        "timestamp": now.isoformat(),

        "timezone": (
            "Asia/Bangkok"
        ),

        "locations": processed,

        "current_summary": (
            current_summary
        ),
    }

    atomic_write_json(
        OUTPUT_FILE,
        weather_output,
    )

    print(
        f"Saved weather data to "
        f"{OUTPUT_FILE}"
    )

    # -------------------------------------------------------------------------
    # Print status
    # -------------------------------------------------------------------------

    print_current_status(
        current_summary
    )

    # -------------------------------------------------------------------------
    # Create state used for comparison.
    # -------------------------------------------------------------------------

    current_comparison = (
        create_comparison_summary(
            current_summary
        )
    )

    # -------------------------------------------------------------------------
    # Load the LAST SUCCESSFULLY SENT state.
    # -------------------------------------------------------------------------

    last_sent = (
        load_last_sent_summary()
    )

    # =========================================================================
    # FIRST RUN
    # =========================================================================

    if last_sent is None:

        print(
            "No previous sent-summary "
            "state found."
        )

        print(
            "This is the first run."
        )

        message = (
            format_whatsapp_message(
                current_summary
            )
        )

        print()
        print(
            "Initial WhatsApp notification:"
        )

        print(
            "-" * 60
        )

        print(
            message
        )

        print(
            "-" * 60
        )

        if send_whatsapp(
            message
        ):

            # IMPORTANT:
            #
            # Only save the state after
            # successful WhatsApp delivery.

            save_last_sent_summary(
                current_comparison
            )

            print(
                "Saved last-sent summary state."
            )

        else:

            print(
                "WhatsApp send failed."
            )

            print(
                "Last-sent state was NOT updated."
            )

            print(
                "Next check will retry."
            )

        return

    # =========================================================================
    # NO CHANGE
    # =========================================================================

    if (
        current_comparison
        == last_sent
    ):

        print(
            "Weather summary unchanged."
        )

        print(
            "No WhatsApp notification needed."
        )

        return

    # =========================================================================
    # WEATHER CHANGED
    # =========================================================================

    print(
        "Weather summary changed."
    )

    message = (
        format_whatsapp_message(
            current_summary
        )
    )

    print()
    print(
        "Changed weather notification:"
    )

    print(
        "-" * 60
    )

    print(
        message
    )

    print(
        "-" * 60
    )

    if send_whatsapp(
        message
    ):

        # IMPORTANT:
        #
        # Update the comparison state
        # only after successful NATS publish.

        save_last_sent_summary(
            current_comparison
        )

        print(
            "Updated last-sent summary state."
        )

    else:

        print(
            "WhatsApp send failed."
        )

        print(
            "Last-sent state was NOT updated."
        )

        print(
            "Next check will retry."
        )


# =============================================================================
# MAIN LOOP
# =============================================================================

def main():

    print()
    print(
        "Thailand weather monitor started."
    )

    print(
        f"Locations file : "
        f"{LOCATIONS_FILE}"
    )

    print(
        f"Weather output : "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Summary state  : "
        f"{SUMMARY_STATE_FILE}"
    )

    print(
        f"Check interval : "
        f"{CHECK_INTERVAL_SECONDS // 60} minutes"
    )

    print()

    while True:

        try:

            check_weather()

        except KeyboardInterrupt:

            print()
            print(
                "Weather monitor stopped."
            )

            sys.exit(0)

        except Exception as exc:

            print()
            print(
                f"ERROR: {exc}"
            )

            print(
                "Traceback:"
            )

            traceback.print_exc()

        print()
        print(
            f"Next check in "
            f"{CHECK_INTERVAL_SECONDS // 60} minutes..."
        )

        try:

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            print()
            print(
                "Weather monitor stopped."
            )

            sys.exit(0)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()
