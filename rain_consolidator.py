#!/usr/bin/env python3

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# Configuration
# ============================================================

STATUS_FILE = Path("rain_status.json")
STATE_FILE = Path("rain_consolidator_state.json")

CHECK_INTERVAL_SECONDS = 15

# ------------------------------------------------------------
# City rain decision
# ------------------------------------------------------------

# At least this many webcams must report rain before
# city-level rain can be confirmed.
MIN_RAINING_WEBCAMS = 2

# Percentage of definitive webcam observations that must
# report rain.
#
# Example:
#
# 5 webcams:
#   3 rain / 5 known = 60% -> RAIN
#
# 6 webcams:
#   3 rain / 6 known = 50% -> RAIN
#
RAIN_THRESHOLD = 0.50


# ------------------------------------------------------------
# Confirmation / debounce
# ------------------------------------------------------------

RAIN_CONFIRMATIONS = 2
DRY_CONFIRMATIONS = 2


# ------------------------------------------------------------
# Long rain notification
# ------------------------------------------------------------

LONG_RAIN_NOTIFICATION_HOURS = 4


# ------------------------------------------------------------
# Daily summary
# ------------------------------------------------------------

DAILY_SUMMARY_HOUR = 20
DAILY_SUMMARY_MINUTE = 0


# ------------------------------------------------------------
# Stale data
# ------------------------------------------------------------

STALE_AFTER_MINUTES = 30


# ------------------------------------------------------------
# WhatsApp / NATS
# ------------------------------------------------------------

WHATSAPP_GROUP = "120363412208069172@g.us"
NATS_SUBJECT = "notify.whatsapp"


BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


# ============================================================
# Default state
# ============================================================

DEFAULT_STATE = {
    "city_state": "UNKNOWN",

    "rain_confirmations": 0,
    "dry_confirmations": 0,

    "rain_started_at": None,
    "last_long_rain_notification": None,

    "last_observation_timestamp": None,

    "last_daily_summary_date": None,

    "rain_observations": 0,
    "dry_observations": 0,
    "possible_rain_observations": 0,
    "unknown_observations": 0,
}


# ============================================================
# State
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return DEFAULT_STATE.copy()

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:

            state = json.load(f)

        # Add fields introduced by newer versions.
        for key, value in DEFAULT_STATE.items():
            state.setdefault(key, value)

        return state

    except Exception as e:

        print(
            f"WARNING: Could not load state: {e}"
        )

        return DEFAULT_STATE.copy()


def save_state(state):

    tmp_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    with tmp_file.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
        )

    tmp_file.replace(STATE_FILE)


# ============================================================
# Time
# ============================================================

def now_bangkok():

    return datetime.now(
        BANGKOK_TZ
    )


def parse_timestamp(timestamp):

    try:

        dt = datetime.fromisoformat(
            timestamp
        )

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=BANGKOK_TZ
            )

        return dt

    except Exception:
        return None


# ============================================================
# WhatsApp
# ============================================================

def send_whatsapp(message):

    payload = {
        "to": WHATSAPP_GROUP,
        "text": message,
    }

    try:

        result = subprocess.run(
            [
                "nats",
                "pub",
                NATS_SUBJECT,
                json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:

            print(
                "WhatsApp notification sent."
            )

            return True

        print(
            "ERROR: WhatsApp notification failed: "
            f"{result.stderr.strip()}"
        )

    except Exception as e:

        print(
            f"ERROR sending WhatsApp notification: {e}"
        )

    return False


# ============================================================
# Webcam aggregation
# ============================================================

def classify_city_condition(observation):

    webcams = observation.get(
        "webcams",
        [],
    )

    if not webcams:
        return "UNKNOWN"

    rain_count = 0
    dry_count = 0
    possible_count = 0
    unknown_count = 0

    for webcam in webcams:

        if webcam.get("rain") is True:

            rain_count += 1

        elif webcam.get("dry") is True:

            dry_count += 1

        elif webcam.get("possible_rain") is True:

            possible_count += 1

        else:

            unknown_count += 1

    total_webcams = len(webcams)

    # --------------------------------------------------------
    # City-level RAIN
    # --------------------------------------------------------
    #
    # Require both:
    #
    # 1. Minimum number of raining webcams
    # 2. Required percentage of definitive observations
    #
    definitive_count = (
        rain_count + dry_count
    )

    if definitive_count > 0:

        rain_ratio = (
            rain_count / definitive_count
        )

        if (
            rain_count >= MIN_RAINING_WEBCAMS
            and rain_ratio >= RAIN_THRESHOLD
        ):

            print(
                f"  Aggregation: "
                f"{rain_count}/{definitive_count} "
                f"definitive webcams report rain "
                f"({rain_ratio:.0%})"
            )

            return "RAIN"

    # --------------------------------------------------------
    # City-level DRY
    # --------------------------------------------------------
    #
    # We require all definitive webcams to be dry and
    # at least two webcams to agree.
    #
    if (
        dry_count >= 2
        and rain_count == 0
        and possible_count == 0
    ):

        if dry_count == definitive_count:

            print(
                f"  Aggregation: "
                f"{dry_count}/{definitive_count} "
                f"definitive webcams report dry"
            )

            return "DRY"

    # --------------------------------------------------------
    # Possible/localized rain
    # --------------------------------------------------------

    if rain_count > 0:

        print(
            f"  Aggregation: "
            f"{rain_count} rain, "
            f"{dry_count} dry, "
            f"{possible_count} possible, "
            f"{unknown_count} unknown"
        )

        return "POSSIBLE_RAIN"

    if possible_count > 0:

        print(
            f"  Aggregation: "
            f"{possible_count} possible-rain webcams"
        )

        return "POSSIBLE_RAIN"

    # --------------------------------------------------------
    # Unknown
    # --------------------------------------------------------

    print(
        f"  Aggregation: "
        f"{rain_count} rain, "
        f"{dry_count} dry, "
        f"{possible_count} possible, "
        f"{unknown_count} unknown "
        f"out of {total_webcams}"
    )

    return "UNKNOWN"


# ============================================================
# Notifications
# ============================================================

def handle_rain_started(state):

    now = now_bangkok()

    state["rain_started_at"] = (
        now.isoformat()
    )

    state["last_long_rain_notification"] = (
        now.isoformat()
    )

    message = (
        "☔ Bangkok rain detected.\n\n"
        "Rain is currently confirmed across "
        "multiple monitored webcams."
    )

    send_whatsapp(message)


def handle_rain_stopped(state):

    duration_text = ""

    rain_started_at = state.get(
        "rain_started_at"
    )

    if rain_started_at:

        started = parse_timestamp(
            rain_started_at
        )

        if started:

            duration = (
                now_bangkok() - started
            )

            total_minutes = max(
                0,
                int(
                    duration.total_seconds()
                    / 60
                ),
            )

            hours = total_minutes // 60
            minutes = total_minutes % 60

            if hours:

                duration_text = (
                    f"\nEstimated duration: "
                    f"{hours}h {minutes}m."
                )

            else:

                duration_text = (
                    f"\nEstimated duration: "
                    f"{minutes} minutes."
                )

    message = (
        "🌤️ Bangkok rain appears to have stopped."
        f"{duration_text}"
    )

    send_whatsapp(message)

    state["rain_started_at"] = None
    state["last_long_rain_notification"] = None


def maybe_send_long_rain_notification(state):

    if state["city_state"] != "RAIN":
        return

    rain_started_at = state.get(
        "rain_started_at"
    )

    if not rain_started_at:
        return

    started = parse_timestamp(
        rain_started_at
    )

    if not started:
        return

    now = now_bangkok()

    if (
        now - started
        < timedelta(
            hours=LONG_RAIN_NOTIFICATION_HOURS
        )
    ):
        return

    last_notification = state.get(
        "last_long_rain_notification"
    )

    if last_notification:

        last = parse_timestamp(
            last_notification
        )

        if last:

            if (
                now - last
                < timedelta(
                    hours=LONG_RAIN_NOTIFICATION_HOURS
                )
            ):
                return

    message = (
        "☔ Bangkok rain continues.\n\n"
        f"Rain has remained confirmed for "
        f"at least "
        f"{LONG_RAIN_NOTIFICATION_HOURS} hours."
    )

    if send_whatsapp(message):

        state["last_long_rain_notification"] = (
            now.isoformat()
        )


# ============================================================
# Daily summary
# ============================================================

def maybe_send_daily_summary(state):

    now = now_bangkok()

    if (
        now.hour != DAILY_SUMMARY_HOUR
        or now.minute != DAILY_SUMMARY_MINUTE
    ):
        return

    today = now.date().isoformat()

    if (
        state.get("last_daily_summary_date")
        == today
    ):
        return

    message = (
        "🇹🇭 Bangkok rain summary\n\n"
        f"☔ Confirmed rain observations: "
        f"{state.get('rain_observations', 0)}\n"
        f"🌤️ Dry observations: "
        f"{state.get('dry_observations', 0)}\n"
        f"🌦️ Possible/localized rain: "
        f"{state.get('possible_rain_observations', 0)}\n"
        f"❓ Unknown observations: "
        f"{state.get('unknown_observations', 0)}"
    )

    if send_whatsapp(message):

        state["last_daily_summary_date"] = today


# ============================================================
# Process one NEW observation
# ============================================================

def process_observation(
    observation,
    state,
):

    timestamp = observation.get(
        "timestamp"
    )

    if not timestamp:

        print(
            "WARNING: Observation has no timestamp."
        )

        return

    observation_time = parse_timestamp(
        timestamp
    )

    if not observation_time:

        print(
            f"WARNING: Invalid timestamp: "
            f"{timestamp}"
        )

        return

    # --------------------------------------------------------
    # Stale observation
    # --------------------------------------------------------

    age = (
        now_bangkok()
        - observation_time
    )

    if age > timedelta(
        minutes=STALE_AFTER_MINUTES
    ):

        print(
            f"Observation is stale "
            f"({int(age.total_seconds() / 60)} "
            f"minutes old)."
        )

        return

    # --------------------------------------------------------
    # Aggregate webcams
    # --------------------------------------------------------

    city_condition = classify_city_condition(
        observation
    )

    print(
        f"[{now_bangkok().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Observed city condition: "
        f"{city_condition}"
    )

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    if city_condition == "RAIN":

        state["rain_observations"] += 1

    elif city_condition == "DRY":

        state["dry_observations"] += 1

    elif city_condition == "POSSIBLE_RAIN":

        state["possible_rain_observations"] += 1

    else:

        state["unknown_observations"] += 1

    # ========================================================
    # RAIN
    # ========================================================

    if city_condition == "RAIN":

        # Break dry confirmation sequence.
        state["dry_confirmations"] = 0

        # Already confirmed rain.
        if state["city_state"] == "RAIN":

            print(
                "City already confirmed RAIN."
            )

            state["rain_confirmations"] = 0

            maybe_send_long_rain_notification(
                state
            )

            return

        # Start/continue rain confirmation.
        state["rain_confirmations"] += 1

        print(
            f"Rain confirmation: "
            f"{state['rain_confirmations']}/"
            f"{RAIN_CONFIRMATIONS}"
        )

        if (
            state["rain_confirmations"]
            >= RAIN_CONFIRMATIONS
        ):

            previous_state = (
                state["city_state"]
            )

            state["city_state"] = "RAIN"

            state["rain_confirmations"] = 0

            if previous_state != "RAIN":

                handle_rain_started(
                    state
                )

        return

    # ========================================================
    # DRY
    # ========================================================

    if city_condition == "DRY":

        # Break rain confirmation sequence.
        state["rain_confirmations"] = 0

        # Already confirmed dry.
        if state["city_state"] == "DRY":

            print(
                "City already confirmed DRY."
            )

            state["dry_confirmations"] = 0

            return

        # Start/continue dry confirmation.
        state["dry_confirmations"] += 1

        print(
            f"Dry confirmation: "
            f"{state['dry_confirmations']}/"
            f"{DRY_CONFIRMATIONS}"
        )

        if (
            state["dry_confirmations"]
            >= DRY_CONFIRMATIONS
        ):

            previous_state = (
                state["city_state"]
            )

            state["city_state"] = "DRY"

            state["dry_confirmations"] = 0

            if previous_state == "RAIN":

                handle_rain_stopped(
                    state
                )

        return

    # ========================================================
    # POSSIBLE RAIN
    # ========================================================

    if city_condition == "POSSIBLE_RAIN":

        print(
            "Possible/localized rain. "
            "No city-level state change."
        )

        state["rain_confirmations"] = 0
        state["dry_confirmations"] = 0

        return

    # ========================================================
    # UNKNOWN
    # ========================================================

    print(
        "Unknown condition. "
        "No city-level state change."
    )

    state["rain_confirmations"] = 0
    state["dry_confirmations"] = 0


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 70)
    print("Bangkok Rain Consolidator")
    print("=" * 70)
    print()

    print(
        f"Rain threshold: "
        f"{RAIN_THRESHOLD:.0%}"
    )

    print(
        f"Minimum raining webcams: "
        f"{MIN_RAINING_WEBCAMS}"
    )

    print(
        f"Rain confirmations: "
        f"{RAIN_CONFIRMATIONS}"
    )

    print(
        f"Dry confirmations: "
        f"{DRY_CONFIRMATIONS}"
    )

    print()

    state = load_state()

    print(
        f"Current city state: "
        f"{state['city_state']}"
    )

    if state.get(
        "last_observation_timestamp"
    ):

        print(
            "Last processed observation: "
            f"{state['last_observation_timestamp']}"
        )

    print()

    while True:

        try:

            if not STATUS_FILE.exists():

                print(
                    "No rain_status.json yet."
                )

                time.sleep(
                    CHECK_INTERVAL_SECONDS
                )

                continue

            try:

                with STATUS_FILE.open(
                    "r",
                    encoding="utf-8",
                ) as f:

                    observation = json.load(f)

            except Exception as e:

                print(
                    f"ERROR reading rain_status.json: "
                    f"{e}"
                )

                time.sleep(
                    CHECK_INTERVAL_SECONDS
                )

                continue

            observation_timestamp = (
                observation.get(
                    "timestamp"
                )
            )

            if not observation_timestamp:

                print(
                    "rain_status.json has no timestamp."
                )

                time.sleep(
                    CHECK_INTERVAL_SECONDS
                )

                continue

            # =================================================
            # CRITICAL DEDUPLICATION
            # =================================================

            if (
                observation_timestamp
                == state.get(
                    "last_observation_timestamp"
                )
            ):

                print(
                    "No new LLM observation."
                )

            else:

                print()
                print(
                    f"New observation: "
                    f"{observation_timestamp}"
                )

                process_observation(
                    observation,
                    state,
                )

                # Mark this exact LLM observation as
                # processed.
                state[
                    "last_observation_timestamp"
                ] = observation_timestamp

                save_state(state)

            # -------------------------------------------------
            # Long-rain notification
            # -------------------------------------------------

            maybe_send_long_rain_notification(
                state
            )

            # -------------------------------------------------
            # Daily summary
            # -------------------------------------------------

            maybe_send_daily_summary(
                state
            )

            save_state(state)

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            print()
            print(
                "Stopping Bangkok Rain Consolidator..."
            )

            save_state(state)

            break

        except Exception as e:

            print(
                f"ERROR in consolidator loop: "
                f"{type(e).__name__}: {e}"
            )

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )


if __name__ == "__main__":
    main()