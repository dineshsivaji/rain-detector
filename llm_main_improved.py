#!/usr/bin/env python3

import base64
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import requests
import yt_dlp


# ============================================================
# Configuration
# ============================================================

WEBCAM_FILE = Path("webcams.txt")

LLM_URL = "http://127.0.0.1:8081/v1/chat/completions"
MODEL_NAME = "gemma-4-e2b"

YOUTUBE_FORMAT = "bestvideo[height<=720]"

COOKIE_FILE = Path.home() / ".config/yt-dlp/youtube-cookies.txt"

VIDEO_DURATION_SECONDS = 10

# Capture these timestamps from each webcam.
FRAME_TIMES = [0, 5, 10]

CHECK_INTERVAL_SECONDS = 300

FRAME_DIR = Path("rain_frames")
STATUS_FILE = Path("rain_status.json")

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

JPEG_QUALITY = 90

# Minimum confidence required before accepting a definitive
# rain/dry classification from the model.
RAIN_CONFIDENCE_THRESHOLD = 0.70


# ============================================================
# Prompts
# ============================================================

SYSTEM_PROMPT = """
You are a visual weather observation system.

Your task is to determine whether ACTIVE RAIN is visibly occurring
in the webcam frames.

IMPORTANT:

- Detect CURRENT ACTIVE RAIN.
- Do NOT classify a scene as rain merely because:
  - the road is wet
  - the ground is wet
  - there are puddles
  - the sky is dark
  - clouds are present
  - there was rain earlier
  - vehicles have water on them

Strong evidence for active rain includes:
- visible falling rain streaks
- visible droplets falling through the scene
- people currently using umbrellas because of rain
- clearly visible rain falling from the sky
- other direct visual evidence of current rainfall

If the scene appears dry and there is no evidence of active rain,
classify it as dry.

If the evidence is ambiguous, classify it as possible_rain or unknown.

Return ONLY valid JSON.
"""


USER_PROMPT = """
Analyze these webcam frames chronologically.

Determine whether active rain is occurring NOW.

The frames are from the same webcam and may be several seconds apart.

Use the change between frames as additional evidence.

Return exactly this JSON structure:

{
  "condition": "rain|dry|possible_rain|unknown",
  "rain": true|false,
  "possible_rain": true|false,
  "dry": true|false,
  "unknown": true|false,
  "confidence": 0.0,
  "evidence": "short explanation",
  "cues": [
    "short visual cue",
    "short visual cue"
  ]
}

Rules:

- "rain": true only when active rain is visually supported.
- "dry": true when there is no visible active rain and the scene appears dry.
- "possible_rain": true when rain is plausible but not sufficiently visible to confirm.
- "unknown": true when the frames do not provide enough useful evidence.
- Only one of rain/dry/possible_rain/unknown should be true.
- confidence must be between 0.0 and 1.0.
"""


# ============================================================
# Webcam configuration
# ============================================================

def load_webcams():
    """
    Load webcam definitions from webcams.txt.

    Format:

        Webcam 1|https://www.youtube.com/watch?v=...
        Webcam 2|https://www.youtube.com/watch?v=...

    Empty lines and lines beginning with # are ignored.
    """

    if not WEBCAM_FILE.exists():
        raise FileNotFoundError(
            f"Webcam configuration file not found: {WEBCAM_FILE}"
        )

    webcams = []

    with WEBCAM_FILE.open("r", encoding="utf-8") as f:

        for line_number, raw_line in enumerate(f, start=1):

            line = raw_line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            if "|" not in line:
                raise ValueError(
                    f"Invalid webcams.txt line {line_number}: "
                    f"{line}\n"
                    f"Expected: Webcam Name|YouTube URL"
                )

            name, url = line.split("|", 1)

            name = name.strip()
            url = url.strip()

            if not name or not url:
                raise ValueError(
                    f"Invalid webcams.txt line {line_number}: {line}"
                )

            webcams.append({
                "name": name,
                "url": url,
            })

    if not webcams:
        raise ValueError(
            f"No webcams configured in {WEBCAM_FILE}"
        )

    return webcams


# ============================================================
# YouTube stream
# ============================================================

def get_youtube_stream(url):
    """
    Resolve a YouTube URL into an HLS stream URL.
    """

    ydl_opts = {
        "format": YOUTUBE_FORMAT,
        "quiet": True,
        "no_warnings": True,
        "cookiefile": str(COOKIE_FILE),
        "js_runtimes": {
            "node": {}
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(
            url,
            download=False,
        )

        stream_url = info.get("url")

        if not stream_url:
            raise RuntimeError(
                "yt-dlp did not return a stream URL"
            )

        return stream_url


# ============================================================
# Frame capture
# ============================================================

def capture_frames(stream_url, webcam_name):
    """
    Capture frames from the HLS stream.

    Returns a list of image paths.
    """

    safe_name = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in webcam_name
    )

    webcam_dir = FRAME_DIR / safe_name
    webcam_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    cap = cv2.VideoCapture(stream_url)

    if not cap.isOpened():
        raise RuntimeError(
            f"Could not open video stream for {webcam_name}"
        )

    frames = []

    start_time = time.monotonic()

    next_frame_index = 0

    try:

        while next_frame_index < len(FRAME_TIMES):

            ret, frame = cap.read()

            if not ret:
                raise RuntimeError(
                    f"Could not read frame from {webcam_name}"
                )

            elapsed = time.monotonic() - start_time

            target_time = FRAME_TIMES[next_frame_index]

            if elapsed >= target_time:

                frame_path = (
                    webcam_dir
                    / f"frame_{next_frame_index}.jpg"
                )

                success = cv2.imwrite(
                    str(frame_path),
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        JPEG_QUALITY,
                    ],
                )

                if not success:
                    raise RuntimeError(
                        f"Could not save frame: {frame_path}"
                    )

                frames.append(frame_path)

                next_frame_index += 1

            # Safety timeout.
            if elapsed > VIDEO_DURATION_SECONDS + 15:
                raise RuntimeError(
                    f"Timed out capturing frames from "
                    f"{webcam_name}"
                )

    finally:
        cap.release()

    return frames


# ============================================================
# Image encoding
# ============================================================

def image_to_data_url(image_path):

    with open(image_path, "rb") as f:
        image_data = base64.b64encode(
            f.read()
        ).decode("utf-8")

    return f"data:image/jpeg;base64,{image_data}"


# ============================================================
# LLM analysis
# ============================================================

def analyze_frames(frame_paths):

    content = [
        {
            "type": "text",
            "text": USER_PROMPT,
        }
    ]

    for frame_path in frame_paths:

        content.append({
            "type": "image_url",
            "image_url": {
                "url": image_to_data_url(frame_path)
            }
        })

    payload = {
        "model": MODEL_NAME,

        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": content,
            },
        ],

        "temperature": 0,

        "max_tokens": 512,

        "reasoning_effort": "none",

        "chat_template_kwargs": {
            "enable_thinking": False
        },

        "reasoning_format": "none",

        "response_format": {
            "type": "json_object"
        },
    }

    response = requests.post(
        LLM_URL,
        json=payload,
        timeout=180,
    )

    response.raise_for_status()

    data = response.json()

    content = data["choices"][0]["message"]["content"]

    result = json.loads(content)

    return result


# ============================================================
# Normalize LLM result
# ============================================================

def normalize_result(result):

    condition = str(
        result.get("condition", "unknown")
    ).lower().strip()

    if condition not in {
        "rain",
        "dry",
        "possible_rain",
        "unknown",
    }:
        condition = "unknown"

    try:
        confidence = float(
            result.get("confidence", 0.0)
        )
    except Exception:
        confidence = 0.0

    confidence = max(
        0.0,
        min(1.0, confidence),
    )

    # Enforce our confidence threshold for definitive rain.
    if (
        condition == "rain"
        and confidence < RAIN_CONFIDENCE_THRESHOLD
    ):
        condition = "possible_rain"

    return {
        "condition": condition,

        "rain": condition == "rain",

        "possible_rain": (
            condition == "possible_rain"
        ),

        "dry": condition == "dry",

        "unknown": condition == "unknown",

        "confidence": confidence,

        "evidence": str(
            result.get("evidence", "")
        ),

        "cues": result.get("cues", []),
    }


# ============================================================
# Save status
# ============================================================

def save_rain_status(webcam_results):

    now = datetime.now(
        BANGKOK_TZ
    ).isoformat()

    status = {
        "timestamp": now,
        "webcams": webcam_results,
    }

    tmp_file = STATUS_FILE.with_suffix(
        ".tmp"
    )

    with tmp_file.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            status,
            f,
            indent=2,
            ensure_ascii=False,
        )

    tmp_file.replace(STATUS_FILE)

    return status


# ============================================================
# Cleanup
# ============================================================

def cleanup_frames(frame_paths):

    for frame_path in frame_paths:

        try:
            frame_path.unlink(
                missing_ok=True
            )

        except Exception as e:

            print(
                f"WARNING: Could not remove "
                f"{frame_path}: {e}"
            )


# ============================================================
# One complete analysis cycle
# ============================================================

def run_cycle(webcams):

    print()
    print("=" * 70)
    print(
        f"Starting rain analysis cycle "
        f"({len(webcams)} webcams)"
    )
    print("=" * 70)

    webcam_results = []

    cycle_start = time.monotonic()

    for index, webcam in enumerate(
        webcams,
        start=1,
    ):

        name = webcam["name"]
        url = webcam["url"]

        print()
        print(
            f"[{index}/{len(webcams)}] {name}"
        )

        frame_paths = []

        try:

            print("  Resolving YouTube stream...")

            stream_url = get_youtube_stream(url)

            print("  Capturing frames...")

            frame_paths = capture_frames(
                stream_url,
                name,
            )

            print(
                f"  Captured {len(frame_paths)} frames."
            )

            print("  Sending frames to Gemma...")

            analysis_start = time.monotonic()

            result = analyze_frames(
                frame_paths
            )

            elapsed = (
                time.monotonic()
                - analysis_start
            )

            result = normalize_result(
                result
            )

            result["name"] = name

            webcam_results.append(
                result
            )

            print(
                f"  Result: {result['condition']} "
                f"(confidence={result['confidence']:.2f})"
            )

            print(
                f"  LLM time: {elapsed:.1f}s"
            )

            print(
                f"  Evidence: {result['evidence']}"
            )

        except Exception as e:

            print(
                f"  ERROR processing {name}: "
                f"{type(e).__name__}: {e}"
            )

            # Preserve the webcam in the status file even
            # when its stream or analysis fails.
            webcam_results.append({
                "name": name,
                "condition": "unknown",
                "rain": False,
                "possible_rain": False,
                "dry": False,
                "unknown": True,
                "confidence": 0.0,
                "evidence": (
                    f"Webcam analysis failed: {e}"
                ),
                "cues": [],
            })

        finally:

            cleanup_frames(
                frame_paths
            )

    status = save_rain_status(
        webcam_results
    )

    cycle_elapsed = (
        time.monotonic()
        - cycle_start
    )

    print()
    print(
        f"Cycle completed in "
        f"{cycle_elapsed:.1f}s"
    )

    # Summary
    rain_count = sum(
        1
        for webcam in webcam_results
        if webcam["rain"]
    )

    dry_count = sum(
        1
        for webcam in webcam_results
        if webcam["dry"]
    )

    possible_count = sum(
        1
        for webcam in webcam_results
        if webcam["possible_rain"]
    )

    unknown_count = sum(
        1
        for webcam in webcam_results
        if webcam["unknown"]
    )

    print(
        f"Summary: "
        f"rain={rain_count}, "
        f"dry={dry_count}, "
        f"possible={possible_count}, "
        f"unknown={unknown_count}"
    )

    return status


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 70)
    print("Bangkok Rain Detector")
    print("=" * 70)

    webcams = load_webcams()

    print(
        f"Loaded {len(webcams)} webcams "
        f"from {WEBCAM_FILE}"
    )

    for webcam in webcams:
        print(
            f"  - {webcam['name']}: "
            f"{webcam['url']}"
        )

    while True:

        try:

            # Reload configuration every cycle.
            #
            # This means you can add/remove webcams from
            # webcams.txt without modifying the Python code.
            webcams = load_webcams()

            run_cycle(webcams)

            print()
            print(
                f"Sleeping for "
                f"{CHECK_INTERVAL_SECONDS} seconds..."
            )

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            print()
            print(
                "Stopping rain detector..."
            )

            break

        except Exception as e:

            print()
            print(
                f"ERROR: {type(e).__name__}: {e}"
            )

            print(
                f"Retrying in "
                f"{CHECK_INTERVAL_SECONDS} seconds..."
            )

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )


if __name__ == "__main__":
    main()