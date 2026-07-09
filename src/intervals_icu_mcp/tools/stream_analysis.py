"""Derived analysis from Intervals.icu activity streams."""

import base64
import csv
import hashlib
import io
import json
from statistics import mean
from typing import Annotated, Any
import zipfile

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..response_builder import ResponseBuilder


STREAMS = [
    "time",
    "distance",
    "watts",
    "power",
    "heartrate",
    "cadence",
    "velocity_smooth",
    "altitude",
    "grade_smooth",
    "moving",
]

CANONICAL_COLUMNS = [
    "index",
    "timestamp",
    "elapsed_s",
    "moving_s",
    "distance_m",
    "lat",
    "lon",
    "altitude_m",
    "velocity_mps",
    "speed_kmh",
    "watts",
    "heartrate",
    "cadence",
    "temperature",
    "grade",
    "vertical_speed",
    "pace",
    "gap",
    "is_moving",
]


def _all_streams(streams_data: Any) -> dict[str, Any]:
    if hasattr(streams_data, "model_dump"):
        payload = streams_data.model_dump(exclude_none=True)
    else:
        payload = getattr(streams_data, "__dict__", {})
    return {key: value for key, value in payload.items() if value is not None}


def _seq(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _valid_numbers(values: list[Any]) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float))]


def _avg(values: list[Any]) -> float | None:
    nums = _valid_numbers(values)
    return round(mean(nums), 2) if nums else None


def _safe(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _format_pace(sec_per_km: float | None) -> str | None:
    if sec_per_km is None or sec_per_km <= 0:
        return None
    seconds = int(round(sec_per_km))
    return f"{seconds // 60}:{seconds % 60:02d}/km"


def _pace_from_speed(speed_mps: float | None) -> float | None:
    if speed_mps is None or speed_mps <= 0:
        return None
    return 1000.0 / speed_mps


def _json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _build_rows(streams_data: Any) -> list[dict[str, Any]]:
    all_streams = _all_streams(streams_data)
    time = _seq(getattr(streams_data, "time", None))
    distance = _seq(getattr(streams_data, "distance", None))
    hr = _seq(getattr(streams_data, "heartrate", None))
    cadence = _seq(getattr(streams_data, "cadence", None))
    watts = _seq(getattr(streams_data, "watts", None))
    power_alias = _seq(getattr(streams_data, "power", None))
    raw_watts = _seq(getattr(streams_data, "raw_watts", None))
    fixed_watts = _seq(getattr(streams_data, "fixed_watts", None))
    power = watts or power_alias or raw_watts or fixed_watts
    speed = _seq(getattr(streams_data, "velocity_smooth", None))
    altitude = _seq(getattr(streams_data, "altitude", None))
    grade = _seq(getattr(streams_data, "grade_smooth", None))
    moving = _seq(getattr(streams_data, "moving", None))

    list_streams = {name: value for name, value in all_streams.items() if isinstance(value, list)}
    length = max([len(value) for value in list_streams.values()] or [0])
    rows: list[dict[str, Any]] = []
    moving_s = 0
    for i in range(length):
        speed_value = _safe(speed[i]) if i < len(speed) else None
        pace = _pace_from_speed(speed_value)
        is_moving = bool(moving[i]) if i < len(moving) and moving[i] is not None else None
        if is_moving is not False:
            moving_s += 1
        lat = lon = None
        latlng = all_streams.get("latlng")
        if isinstance(latlng, list) and i < len(latlng) and isinstance(latlng[i], list):
            if len(latlng[i]) >= 2:
                lat, lon = latlng[i][0], latlng[i][1]
        temp_stream = all_streams.get("temp") or all_streams.get("temperature")
        rows.append(
            {
                "index": i,
                "timestamp": None,
                "second": int(time[i]) if i < len(time) and isinstance(time[i], (int, float)) else i,
                "elapsed_s": int(time[i]) if i < len(time) and isinstance(time[i], (int, float)) else i,
                "moving_s": moving_s,
                "distance_m": round(float(distance[i]), 1)
                if i < len(distance) and isinstance(distance[i], (int, float))
                else None,
                "lat": lat,
                "lon": lon,
                "pace_sec_per_km": round(pace, 1) if pace else None,
                "pace": _format_pace(pace),
                "hr": int(hr[i]) if i < len(hr) and isinstance(hr[i], (int, float)) else None,
                "heartrate": int(hr[i]) if i < len(hr) and isinstance(hr[i], (int, float)) else None,
                "cadence": round(float(cadence[i]), 1)
                if i < len(cadence) and isinstance(cadence[i], (int, float))
                else None,
                "watts": int(power[i]) if i < len(power) and isinstance(power[i], (int, float)) else None,
                "speed_mps": round(speed_value, 3) if speed_value is not None else None,
                "velocity_mps": round(speed_value, 3) if speed_value is not None else None,
                "speed_kmh": round(speed_value * 3.6, 2) if speed_value is not None else None,
                "altitude_m": round(float(altitude[i]), 1)
                if i < len(altitude) and isinstance(altitude[i], (int, float))
                else None,
                "grade_pct": round(float(grade[i]), 1)
                if i < len(grade) and isinstance(grade[i], (int, float))
                else None,
                "grade": round(float(grade[i]), 1)
                if i < len(grade) and isinstance(grade[i], (int, float))
                else None,
                "temperature": temp_stream[i]
                if isinstance(temp_stream, list) and i < len(temp_stream)
                else None,
                "vertical_speed": None,
                "gap": None,
                "moving": is_moving,
                "is_moving": is_moving,
            }
        )
        for stream_name, stream_values in list_streams.items():
            if stream_name in rows[-1]:
                continue
            if len(stream_values) == length and i < len(stream_values):
                rows[-1][stream_name] = stream_values[i]
    return rows


def _columns_for_rows(rows: list[dict[str, Any]], requested: list[str] | None = None) -> list[str]:
    if requested:
        return requested
    keys = {key for row in rows for key in row.keys()}
    columns = [col for col in CANONICAL_COLUMNS if col in keys]
    extras = sorted(keys - set(columns) - {"second", "hr", "speed_mps", "grade_pct", "moving"})
    return columns + extras


def _rows_as_arrays(rows: list[dict[str, Any]], columns: list[str]) -> list[list[Any]]:
    return [[row.get(col) for col in columns] for row in rows]


def _parse_json_list(value: str | None, field_name: str) -> list[str] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{field_name} must be a JSON array of strings")
    return parsed


def _parse_json_dict(value: str | None, field_name: str) -> dict[str, list[str]] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    result: dict[str, list[str]] = {}
    for key, val in parsed.items():
        if not isinstance(key, str) or not isinstance(val, list) or not all(isinstance(item, str) for item in val):
            raise ValueError(f"{field_name} must map column names to arrays of aggregation names")
        result[key] = val
    return result


def _column_unit(column: str) -> str | None:
    lower = column.lower()
    if lower in {"index"}:
        return "row"
    if lower in {"second", "elapsed_s", "moving_s"} or lower.endswith("_s"):
        return "s"
    if "distance" in lower:
        return "m"
    if lower in {"lat", "lon"}:
        return "deg"
    if "altitude" in lower or "elevation" in lower:
        return "m"
    if lower in {"velocity_mps", "speed_mps"}:
        return "m/s"
    if lower == "speed_kmh":
        return "km/h"
    if "watt" in lower or lower == "power":
        return "W"
    if lower in {"hr", "heartrate", "heart_rate"} or "heartrate" in lower:
        return "bpm"
    if "cadence" in lower:
        return "rpm_or_spm"
    if "grade" in lower:
        return "%"
    if "temperature" in lower or lower == "temp":
        return "C"
    if "pace" in lower:
        return "sec/km_or_text"
    return None


def _column_profiles(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    profiles = []
    for column in columns:
        values = [row.get(column) for row in rows]
        non_null = [value for value in values if value is not None]
        nums = _valid_numbers(non_null)
        sample = []
        for value in non_null:
            if value not in sample:
                sample.append(value)
            if len(sample) >= 3:
                break
        profile: dict[str, Any] = {
            "name": column,
            "unit": _column_unit(column),
            "non_null": len(non_null),
            "numeric": len(nums),
            "type": "numeric" if nums and len(nums) == len(non_null) else "mixed" if nums else "text_or_boolean",
            "sample": sample,
        }
        if nums:
            profile.update(
                {
                    "min": round(min(nums), 3),
                    "max": round(max(nums), 3),
                    "avg": round(mean(nums), 3),
                }
            )
        profiles.append(profile)
    return profiles


def _format_rows(rows: list[dict[str, Any]], columns: list[str], output_format: str) -> dict[str, Any]:
    if output_format == "csv":
        return {"format": "csv", "csv": _csv_from_rows(rows, columns)}
    if output_format == "objects":
        return {"format": "objects", "rows": [{col: row.get(col) for col in columns} for row in rows]}
    return {"format": "arrays", "columns": columns, "rows": _rows_as_arrays(rows, columns)}


def _non_tabular_streams(streams_data: Any, row_count: int) -> dict[str, int]:
    return {
        name: len(value)
        for name, value in _all_streams(streams_data).items()
        if isinstance(value, list) and len(value) != row_count
    }


def _csv_from_rows(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    columns = columns or _columns_for_rows(rows)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _zip_files(files: dict[str, str | bytes]) -> tuple[str, int, str]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    data = buffer.getvalue()
    return base64.b64encode(data).decode("ascii"), len(data), hashlib.sha256(data).hexdigest()


def _moving_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("moving") is not False]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    moving = _moving_rows(rows)
    distances = _valid_numbers([row.get("distance_m") for row in rows])
    speeds = _valid_numbers([row.get("speed_mps") for row in moving])
    paces = [_pace_from_speed(speed) for speed in speeds if speed > 0]
    altitudes = _valid_numbers([row.get("altitude_m") for row in rows])
    elevation_gain = 0.0
    elevation_loss = 0.0
    for before, after in zip(altitudes, altitudes[1:]):
        delta = after - before
        if delta > 0:
            elevation_gain += delta
        else:
            elevation_loss += abs(delta)

    avg_speed = round(mean(speeds), 3) if speeds else None
    avg_pace = _pace_from_speed(avg_speed) if avg_speed else None
    return {
        "points": len(rows),
        "moving_points": len(moving),
        "duration_seconds": rows[-1]["second"] - rows[0]["second"] if len(rows) >= 2 else len(rows),
        "distance_m": round(max(distances), 1) if distances else None,
        "avg_pace_sec_per_km": round(avg_pace, 1) if avg_pace else None,
        "avg_pace": _format_pace(avg_pace),
        "avg_hr": _avg([row.get("hr") for row in moving]),
        "max_hr": max(_valid_numbers([row.get("hr") for row in moving]), default=None),
        "avg_cadence": _avg([row.get("cadence") for row in moving]),
        "avg_watts": _avg([row.get("watts") for row in moving]),
        "max_watts": max(_valid_numbers([row.get("watts") for row in moving]), default=None),
        "elevation_gain_m": round(elevation_gain, 1),
        "elevation_loss_m": round(elevation_loss, 1),
    }


def _splits(rows: list[dict[str, Any]], split_distance_m: int) -> list[dict[str, Any]]:
    if split_distance_m <= 0:
        split_distance_m = 1000
    result: list[dict[str, Any]] = []
    moving = _moving_rows(rows)
    if not moving:
        return result

    start_index = 0
    split_no = 1
    while start_index < len(moving):
        start = moving[start_index]
        start_distance = start.get("distance_m") or 0
        target_distance = start_distance + split_distance_m
        end_index = None
        for i in range(start_index + 1, len(moving)):
            distance = moving[i].get("distance_m")
            if distance is not None and distance >= target_distance:
                end_index = i
                break
        if end_index is None:
            end_index = len(moving) - 1
        if end_index <= start_index:
            break

        chunk = moving[start_index : end_index + 1]
        elapsed = (chunk[-1].get("second") or 0) - (chunk[0].get("second") or 0)
        distance_delta = (chunk[-1].get("distance_m") or 0) - (chunk[0].get("distance_m") or 0)
        pace = (elapsed / distance_delta * 1000) if distance_delta > 0 else None
        result.append(
            {
                "split": split_no,
                "distance_m": round(distance_delta, 1),
                "duration_seconds": int(elapsed),
                "pace_sec_per_km": round(pace, 1) if pace else None,
                "pace": _format_pace(pace),
                "avg_hr": _avg([row.get("hr") for row in chunk]),
                "avg_cadence": _avg([row.get("cadence") for row in chunk]),
                "avg_watts": _avg([row.get("watts") for row in chunk]),
                "elevation_gain_m": _elevation_gain(chunk),
            }
        )
        split_no += 1
        if end_index == len(moving) - 1:
            break
        start_index = end_index
    return result


def _elevation_gain(rows: list[dict[str, Any]]) -> float:
    altitudes = _valid_numbers([row.get("altitude_m") for row in rows])
    gain = 0.0
    for before, after in zip(altitudes, altitudes[1:]):
        if after > before:
            gain += after - before
    return round(gain, 1)


def _hr_drift(rows: list[dict[str, Any]]) -> dict[str, Any]:
    moving = _moving_rows(rows)
    if len(moving) < 60:
        return {"available": False, "reason": "Not enough moving stream points"}
    half = len(moving) // 2
    halves = [moving[:half], moving[half:]]
    data = []
    for chunk in halves:
        speed = _avg([row.get("speed_mps") for row in chunk])
        hr = _avg([row.get("hr") for row in chunk])
        pace = _pace_from_speed(speed) if speed else None
        ratio = (hr / speed) if hr and speed else None
        data.append({"avg_hr": hr, "avg_pace": _format_pace(pace), "hr_per_mps": ratio})
    drift = None
    if data[0]["hr_per_mps"] and data[1]["hr_per_mps"]:
        drift = (data[1]["hr_per_mps"] / data[0]["hr_per_mps"] - 1) * 100
    return {
        "available": drift is not None,
        "first_half": data[0],
        "second_half": data[1],
        "aerobic_decoupling_percent": round(drift, 2) if drift is not None else None,
    }


def _best_efforts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    moving = _moving_rows(rows)
    efforts: dict[str, Any] = {}
    for duration in [60, 300, 600]:
        best = None
        for i in range(0, max(len(moving) - duration, 0)):
            chunk = moving[i : i + duration]
            distance_delta = (chunk[-1].get("distance_m") or 0) - (chunk[0].get("distance_m") or 0)
            if distance_delta <= 0:
                continue
            pace = duration / distance_delta * 1000
            if best is None or pace < best["pace_sec_per_km"]:
                best = {
                    "start_second": chunk[0].get("second"),
                    "end_second": chunk[-1].get("second"),
                    "distance_m": round(distance_delta, 1),
                    "pace_sec_per_km": round(pace, 1),
                    "pace": _format_pace(pace),
                    "avg_hr": _avg([row.get("hr") for row in chunk]),
                    "avg_watts": _avg([row.get("watts") for row in chunk]),
                }
        efforts[f"{duration}_sec"] = best
    return efforts


def _hr_zones(rows: list[dict[str, Any]], zones_json: str | None) -> list[dict[str, Any]]:
    if not zones_json:
        return []
    zones = json.loads(zones_json)
    if not isinstance(zones, list):
        raise ValueError("hr_zones_json must be a JSON array")
    counts = [{"name": z.get("name", f"Z{i + 1}"), "min": z.get("min"), "max": z.get("max"), "seconds": 0} for i, z in enumerate(zones)]
    for row in _moving_rows(rows):
        hr = row.get("hr")
        if not isinstance(hr, (int, float)):
            continue
        for zone in counts:
            low = zone["min"]
            high = zone["max"]
            if (low is None or hr >= low) and (high is None or hr <= high):
                zone["seconds"] += 1
                break
    total = sum(z["seconds"] for z in counts)
    for zone in counts:
        zone["percent"] = round(zone["seconds"] / total * 100, 1) if total else 0
    return counts


def _time_splits(rows: list[dict[str, Any]], split_time_s: int) -> list[dict[str, Any]]:
    moving = _moving_rows(rows)
    if not moving or split_time_s <= 0:
        return []
    result = []
    start_s = int(moving[0].get("elapsed_s") or moving[0].get("second") or 0)
    end_s = int(moving[-1].get("elapsed_s") or moving[-1].get("second") or 0)
    split = 1
    cursor = start_s
    while cursor < end_s:
        chunk = [
            row for row in moving
            if cursor <= int(row.get("elapsed_s") or row.get("second") or 0) < cursor + split_time_s
        ]
        if chunk:
            distance_delta = (chunk[-1].get("distance_m") or 0) - (chunk[0].get("distance_m") or 0)
            pace = split_time_s / distance_delta * 1000 if distance_delta > 0 else None
            result.append(
                {
                    "split": split,
                    "start_s": cursor,
                    "end_s": cursor + split_time_s,
                    "distance_m": round(distance_delta, 1),
                    "pace": _format_pace(pace),
                    "avg_hr": _avg([row.get("heartrate") for row in chunk]),
                    "avg_watts": _avg([row.get("watts") for row in chunk]),
                    "avg_cadence": _avg([row.get("cadence") for row in chunk]),
                }
            )
        split += 1
        cursor += split_time_s
    return result


def _pauses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pauses = []
    start = None
    for row in rows:
        moving = row.get("is_moving")
        second = row.get("elapsed_s") or row.get("second")
        if moving is False and start is None:
            start = second
        elif moving is not False and start is not None:
            if second - start >= 5:
                pauses.append({"start_s": start, "end_s": second, "duration_s": second - start})
            start = None
    return pauses


def _surges(rows: list[dict[str, Any]], ftp: int | None = None) -> list[dict[str, Any]]:
    surges = []
    moving = _moving_rows(rows)
    watts_values = _valid_numbers([row.get("watts") for row in moving])
    if not watts_values:
        return surges
    threshold = ftp * 1.15 if ftp else max(mean(watts_values) * 1.5, sorted(watts_values)[int(len(watts_values) * 0.9)])
    start = None
    peak = 0
    for row in moving:
        watts = row.get("watts")
        second = row.get("elapsed_s") or row.get("second")
        if isinstance(watts, (int, float)) and watts >= threshold:
            start = second if start is None else start
            peak = max(peak, watts)
        elif start is not None:
            if second - start >= 5:
                surges.append({"start_s": start, "end_s": second, "duration_s": second - start, "peak_watts": peak})
            start = None
            peak = 0
    return surges[:50]


def _power_metrics(rows: list[dict[str, Any]], ftp: int | None = None) -> dict[str, Any]:
    moving = _moving_rows(rows)
    values = _valid_numbers([row.get("watts") for row in moving])
    if not values:
        return {
            "available": False,
            "reason": "No numeric watts found in moving stream rows",
            "moving_rows": len(moving),
            "rows_with_watts": 0,
        }
    rows_with_power = [
        row for row in moving
        if isinstance(row.get("watts"), (int, float))
    ]
    rows_with_power_and_hr = [
        row for row in rows_with_power
        if isinstance(row.get("heartrate") or row.get("hr"), (int, float))
    ]
    avg = mean(values)
    hr_values = _valid_numbers([row.get("heartrate") or row.get("hr") for row in rows_with_power_and_hr])
    avg_hr = mean(hr_values) if hr_values else None
    variability = None
    # Approximate normalized power from 30 s rolling averages if enough data exists.
    if len(values) >= 30:
        rolling = [mean(values[i : i + 30]) for i in range(0, len(values) - 29)]
        normalized = mean([v**4 for v in rolling]) ** 0.25
        variability = normalized / avg if avg else None
    else:
        normalized = None
    data = {
        "available": True,
        "avg_watts": round(avg, 1),
        "max_watts": max(values),
        "rows_with_watts": len(rows_with_power),
        "rows_with_watts_and_hr": len(rows_with_power_and_hr),
        "avg_hr_during_power": round(avg_hr, 1) if avg_hr else None,
        "watts_per_bpm": round(avg / avg_hr, 3) if avg_hr else None,
        "estimated_normalized_power": round(normalized, 1) if normalized else None,
        "variability_index": round(variability, 3) if variability else None,
    }
    if ftp:
        data["seconds_above_ftp"] = sum(1 for v in values if v > ftp)
        data["percent_above_ftp"] = round(data["seconds_above_ftp"] / len(values) * 100, 1)
    return data


async def get_activity_data_dictionary(
    activity_id: Annotated[str, "Activity ID to inspect before reading raw data"],
    ctx: Context | None = None,
) -> str:
    """Inspect every available second-by-second field for an activity.

    Use this first whenever the user asks to analyze an activity like a CSV.
    It returns the complete column list, units, non-null counts, samples, and
    recommended next calls. Do not conclude that a field is missing until this
    tool says the field has zero non-null values.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        async with ICUClient(config) as client:
            activity = await client.get_activity(activity_id=activity_id)
            streams_data = await client.get_activity_streams(activity_id)
        rows = _build_rows(streams_data)
        columns = _columns_for_rows(rows)
        data = {
            "activity_id": activity_id,
            "activity": activity.model_dump(mode="json") if hasattr(activity, "model_dump") else {},
            "row_count": len(rows),
            "columns": _column_profiles(rows, columns),
            "available_streams": list(_all_streams(streams_data).keys()),
            "stream_lengths": {
                name: len(value)
                for name, value in _all_streams(streams_data).items()
                if isinstance(value, list)
            },
            "non_tabular_streams": _non_tabular_streams(streams_data, len(rows)),
            "workflow_for_chatgpt": [
                "For full raw data, call read_activity_data_page repeatedly using next_start_index until it is null.",
                "For a specific part of the activity, call read_activity_data_window with start_s/end_s.",
                "For block analysis like every 2 minutes, call aggregate_activity_data with bucket_seconds=120.",
                "Use columns_json to request any exact columns shown here, e.g. [\"elapsed_s\",\"watts\",\"heartrate\",\"cadence\"].",
                "Do not invent missing rows. If a page is needed, fetch the next page.",
            ],
        }
        return ResponseBuilder.build_response(data=data, query_type="activity_data_dictionary")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def read_activity_data_page(
    activity_id: Annotated[str, "Activity ID to read like a CSV"],
    columns_json: Annotated[
        str | None,
        "Optional JSON array of columns to return. Omit to return every available column.",
    ] = None,
    start_index: Annotated[int, "Zero-based row index"] = 0,
    limit: Annotated[int, "Rows to return, capped at 5000"] = 1000,
    output_format: Annotated[str, "arrays, objects, or csv"] = "arrays",
    ctx: Context | None = None,
) -> str:
    """Read one page of the activity as raw rows.

    This is the generic CSV replacement. Use it repeatedly with
    next_start_index to inspect or compute over every second-by-second field.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        requested = _parse_json_list(columns_json, "columns_json")
        if output_format not in {"arrays", "objects", "csv"}:
            raise ValueError("output_format must be arrays, objects, or csv")
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        rows = _build_rows(streams_data)
        columns = _columns_for_rows(rows, requested)
        start_index = max(start_index, 0)
        limit = max(min(limit, 5000), 1)
        page = rows[start_index : start_index + limit]
        next_index = start_index + len(page) if start_index + len(page) < len(rows) else None
        payload = {
            "activity_id": activity_id,
            "start_index": start_index,
            "limit": limit,
            "returned_rows": len(page),
            "total_rows": len(rows),
            "next_start_index": next_index,
            "available_columns": _columns_for_rows(rows),
        }
        payload.update(_format_rows(page, columns, output_format))
        return ResponseBuilder.build_response(data=payload, query_type="activity_data_page")
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid JSON: {str(e)}", error_type="validation_error")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def read_activity_data_window(
    activity_id: Annotated[str, "Activity ID to read like a CSV"],
    start_s: Annotated[int, "Start elapsed second"],
    end_s: Annotated[int, "End elapsed second"],
    columns_json: Annotated[
        str | None,
        "Optional JSON array of columns to return. Omit to return every available column.",
    ] = None,
    output_format: Annotated[str, "arrays, objects, or csv"] = "arrays",
    ctx: Context | None = None,
) -> str:
    """Read raw second-by-second rows for an elapsed-time window."""
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        requested = _parse_json_list(columns_json, "columns_json")
        if output_format not in {"arrays", "objects", "csv"}:
            raise ValueError("output_format must be arrays, objects, or csv")
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        all_rows = _build_rows(streams_data)
        rows = [
            row for row in all_rows
            if start_s <= int(row.get("elapsed_s") or row.get("second") or 0) <= end_s
        ]
        columns = _columns_for_rows(all_rows, requested)
        payload = {
            "activity_id": activity_id,
            "start_s": start_s,
            "end_s": end_s,
            "returned_rows": len(rows),
            "available_columns": _columns_for_rows(all_rows),
        }
        payload.update(_format_rows(rows, columns, output_format))
        return ResponseBuilder.build_response(data=payload, query_type="activity_data_window")
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid JSON: {str(e)}", error_type="validation_error")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def aggregate_activity_data(
    activity_id: Annotated[str, "Activity ID to aggregate like a dataframe"],
    bucket_seconds: Annotated[int | None, "Group rows by elapsed-time bucket size in seconds"] = None,
    bucket_distance_m: Annotated[int | None, "Group rows by distance bucket size in meters"] = None,
    columns_json: Annotated[
        str | None,
        "Optional JSON array of columns. Omit to aggregate every numeric column.",
    ] = None,
    aggregations_json: Annotated[
        str | None,
        "Optional JSON object mapping columns to aggregations, e.g. {\"watts\":[\"avg\",\"max\"],\"heartrate\":[\"avg\"]}. Supported: avg,min,max,sum,first,last,count,delta.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Generic aggregation over any activity stream columns.

    This is intentionally not a coach-specific analysis. It lets ChatGPT do
    CSV-like operations such as every-120-second blocks over any columns
    present in the activity.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        if bucket_seconds and bucket_distance_m:
            raise ValueError("Use either bucket_seconds or bucket_distance_m, not both")
        requested = _parse_json_list(columns_json, "columns_json")
        aggregation_map = _parse_json_dict(aggregations_json, "aggregations_json")
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        rows = _build_rows(streams_data)
        all_columns = _columns_for_rows(rows)
        numeric_columns = [
            column for column in all_columns
            if _valid_numbers([row.get(column) for row in rows])
        ]
        columns = requested or numeric_columns
        default_aggs = ["avg", "min", "max", "first", "last", "count"]
        buckets: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            if bucket_distance_m:
                distance = row.get("distance_m")
                if not isinstance(distance, (int, float)):
                    continue
                bucket = int(distance // bucket_distance_m)
            else:
                elapsed = int(row.get("elapsed_s") or row.get("second") or 0)
                size = bucket_seconds or max(int(rows[-1].get("elapsed_s") or len(rows)), 1)
                bucket = elapsed // size
            buckets.setdefault(bucket, []).append(row)

        result = []
        for bucket, bucket_rows in sorted(buckets.items()):
            if not bucket_rows:
                continue
            item: dict[str, Any] = {
                "bucket": bucket,
                "start_index": bucket_rows[0].get("index"),
                "end_index": bucket_rows[-1].get("index"),
                "start_s": bucket_rows[0].get("elapsed_s"),
                "end_s": bucket_rows[-1].get("elapsed_s"),
                "duration_s": (bucket_rows[-1].get("elapsed_s") or 0) - (bucket_rows[0].get("elapsed_s") or 0),
                "start_distance_m": bucket_rows[0].get("distance_m"),
                "end_distance_m": bucket_rows[-1].get("distance_m"),
                "rows": len(bucket_rows),
            }
            for column in columns:
                aggs = aggregation_map.get(column, default_aggs) if aggregation_map else default_aggs
                values = [row.get(column) for row in bucket_rows]
                nums = _valid_numbers(values)
                non_null = [value for value in values if value is not None]
                for agg in aggs:
                    key = f"{column}_{agg}"
                    if agg == "count":
                        item[key] = len(non_null)
                    elif agg == "first":
                        item[key] = non_null[0] if non_null else None
                    elif agg == "last":
                        item[key] = non_null[-1] if non_null else None
                    elif agg == "delta":
                        item[key] = round(nums[-1] - nums[0], 3) if len(nums) >= 2 else None
                    elif not nums:
                        item[key] = None
                    elif agg == "avg":
                        item[key] = round(mean(nums), 3)
                    elif agg == "min":
                        item[key] = round(min(nums), 3)
                    elif agg == "max":
                        item[key] = round(max(nums), 3)
                    elif agg == "sum":
                        item[key] = round(sum(nums), 3)
                    else:
                        raise ValueError(f"Unsupported aggregation: {agg}")
            result.append(item)

        return ResponseBuilder.build_response(
            data={
                "activity_id": activity_id,
                "grouping": {
                    "bucket_seconds": bucket_seconds,
                    "bucket_distance_m": bucket_distance_m,
                },
                "columns": columns,
                "available_columns": all_columns,
                "buckets": result,
            },
            query_type="activity_data_aggregation",
        )
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid JSON: {str(e)}", error_type="validation_error")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def analyze_activity_streams(
    activity_id: Annotated[str, "Activity ID to analyze from Intervals.icu streams"],
    split_distance_m: Annotated[int, "Split distance in meters. Use 1000 for running km splits."] = 1000,
    hr_zones_json: Annotated[
        str | None,
        "Optional JSON array of HR zones, e.g. [{\"name\":\"Z1\",\"min\":100,\"max\":130}]",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Analyze second-by-second Intervals.icu streams without requiring a CSV upload."""
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        rows = _build_rows(streams_data)
        if not rows:
            return ResponseBuilder.build_response(
                data={"activity_id": activity_id, "available": False},
                metadata={"message": "No stream data available for this activity"},
            )
        data = {
            "activity_id": activity_id,
            "summary": _summary(rows),
            "splits": _splits(rows, split_distance_m),
            "hr_drift": _hr_drift(rows),
            "best_efforts": _best_efforts(rows),
            "hr_zones": _hr_zones(rows, hr_zones_json),
            "available_streams": list(_all_streams(streams_data).keys()),
            "stream_lengths": {
                name: len(value)
                for name, value in _all_streams(streams_data).items()
                if isinstance(value, list)
            },
            "power_stream_available": bool(
                _seq(getattr(streams_data, "watts", None))
                or _seq(getattr(streams_data, "power", None))
                or _seq(getattr(streams_data, "raw_watts", None))
                or _seq(getattr(streams_data, "fixed_watts", None))
            ),
        }
        return ResponseBuilder.build_response(data=data, query_type="activity_stream_analysis")
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(
            f"Invalid hr_zones_json: {str(e)}", error_type="validation_error"
        )
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_activity_streams_page(
    activity_id: Annotated[str, "Activity ID to read streams from"],
    streams_json: Annotated[
        str | None,
        "Optional JSON array of columns/streams to return. If omitted, all available columns are returned.",
    ] = None,
    start_index: Annotated[int, "Zero-based row index to start from"] = 0,
    limit: Annotated[int, "Maximum number of rows to return"] = 1000,
    ctx: Context | None = None,
) -> str:
    """Return a page of second-by-second activity stream rows."""
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        requested = json.loads(streams_json) if streams_json else None
        if requested is not None and not isinstance(requested, list):
            raise ValueError("streams_json must be a JSON array of column names")
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        rows = _build_rows(streams_data)
        columns = _columns_for_rows(rows, requested)
        start_index = max(start_index, 0)
        limit = max(min(limit, 5000), 1)
        page = rows[start_index : start_index + limit]
        next_index = start_index + len(page) if start_index + len(page) < len(rows) else None
        return ResponseBuilder.build_response(
            data={
                "activity_id": activity_id,
                "start_index": start_index,
                "limit": limit,
                "total_rows": len(rows),
                "columns": columns,
                "rows": _rows_as_arrays(page, columns),
                "next_start_index": next_index,
                "available_streams": list(_all_streams(streams_data).keys()),
                "non_tabular_streams": _non_tabular_streams(streams_data, len(rows)),
            },
            query_type="activity_streams_page",
        )
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid streams_json: {str(e)}", error_type="validation_error")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_activity_streams_window(
    activity_id: Annotated[str, "Activity ID to read streams from"],
    start_s: Annotated[int, "Start elapsed second"],
    end_s: Annotated[int, "End elapsed second"],
    streams_json: Annotated[
        str | None,
        "Optional JSON array of columns/streams to return. If omitted, all available columns are returned.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Return second-by-second activity stream rows for a time window."""
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        requested = json.loads(streams_json) if streams_json else None
        if requested is not None and not isinstance(requested, list):
            raise ValueError("streams_json must be a JSON array of column names")
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        rows = [
            row for row in _build_rows(streams_data)
            if start_s <= int(row.get("elapsed_s") or row.get("second") or 0) <= end_s
        ]
        columns = _columns_for_rows(rows, requested)
        return ResponseBuilder.build_response(
            data={
                "activity_id": activity_id,
                "start_s": start_s,
                "end_s": end_s,
                "total_rows": len(rows),
                "columns": columns,
                "rows": _rows_as_arrays(rows, columns),
                "available_streams": list(_all_streams(streams_data).keys()),
                "non_tabular_streams": _non_tabular_streams(streams_data, len(_build_rows(streams_data))),
            },
            query_type="activity_streams_window",
        )
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid streams_json: {str(e)}", error_type="validation_error")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def analyze_activity_full(
    activity_id: Annotated[str, "Activity ID to analyze from Intervals.icu streams"],
    split_distance_m: Annotated[int, "Distance split size in meters"] = 1000,
    split_time_s: Annotated[int, "Time split size in seconds"] = 300,
    ftp: Annotated[int | None, "Optional FTP for power metrics"] = None,
    hr_zones_json: Annotated[str | None, "Optional JSON array of HR zones"] = None,
    ctx: Context | None = None,
) -> str:
    """Return a richer coach-oriented activity analysis from streams."""
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
            activity = await client.get_activity(activity_id=activity_id)
        rows = _build_rows(streams_data)
        data = {
            "activity_id": activity_id,
            "activity": activity.model_dump(mode="json") if hasattr(activity, "model_dump") else {},
            "summary": _summary(rows),
            "distance_splits": _splits(rows, split_distance_m),
            "time_splits": _time_splits(rows, split_time_s),
            "hr_zones": _hr_zones(rows, hr_zones_json),
            "hr_drift": _hr_drift(rows),
            "power": _power_metrics(rows, ftp),
            "best_efforts": _best_efforts(rows),
            "pauses": _pauses(rows),
            "surges": _surges(rows, ftp),
            "available_streams": list(_all_streams(streams_data).keys()),
            "stream_lengths": {
                name: len(value)
                for name, value in _all_streams(streams_data).items()
                if isinstance(value, list)
            },
        }
        return ResponseBuilder.build_response(data=data, query_type="activity_full_analysis")
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid JSON: {str(e)}", error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def export_activity_data(
    activity_id: Annotated[str, "Activity ID to export"],
    include_json: Annotated[
        str | None,
        "Optional JSON array: summary, streams, raw_streams, intervals, best_efforts, original_fit, manifest. Defaults to summary/streams/raw_streams/intervals/best_efforts/manifest.",
    ] = None,
    compress: Annotated[bool, "If true, return a base64 ZIP bundle. If false, return inline text files."] = True,
    include_original_fit: Annotated[bool, "Whether to include the original FIT file if available."] = False,
    ctx: Context | None = None,
) -> str:
    """Export an activity data bundle over MCP.

    A remote Render MCP server cannot write files into ChatGPT's /mnt/data.
    This tool therefore returns either inline text files or a base64 ZIP bundle
    that ChatGPT can decode/use.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")
    try:
        include = json.loads(include_json) if include_json else [
            "summary",
            "streams",
            "raw_streams",
            "intervals",
            "best_efforts",
            "manifest",
        ]
        if not isinstance(include, list):
            raise ValueError("include_json must be a JSON array")
        if include_original_fit and "original_fit" not in include:
            include.append("original_fit")

        files: dict[str, str | bytes] = {}
        rows_count: dict[str, int] = {}
        async with ICUClient(config) as client:
            activity = await client.get_activity(activity_id=activity_id)
            streams_data = await client.get_activity_streams(activity_id)
            rows = _build_rows(streams_data)
            summary = {
                "activity_id": activity_id,
                "activity": activity.model_dump(mode="json") if hasattr(activity, "model_dump") else {},
                "stream_summary": _summary(rows),
                "available_streams": list(_all_streams(streams_data).keys()),
                "stream_lengths": {
                    name: len(value)
                    for name, value in _all_streams(streams_data).items()
                    if isinstance(value, list)
                },
            }
            if "summary" in include:
                files["summary.json"] = _json_dump(summary)
            if "streams" in include:
                files["streams.csv"] = _csv_from_rows(rows)
                rows_count["streams"] = len(rows)
            if "raw_streams" in include:
                files["raw_streams.json"] = _json_dump(_all_streams(streams_data))
            if "intervals" in include:
                intervals = await client.get_activity_intervals(activity_id)
                interval_rows = [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                    for item in intervals
                ]
                files["intervals.json"] = _json_dump(interval_rows)
                rows_count["intervals"] = len(interval_rows)
            if "best_efforts" in include:
                files["best_efforts.json"] = _json_dump(_best_efforts(rows))
            if "original_fit" in include:
                try:
                    files["original.fit"] = await client.download_fit_file(activity_id)
                except ICUAPIError as e:
                    files["original_fit_error.txt"] = e.message

        manifest = {
            "activity_id": activity_id,
            "files": list(files.keys()),
            "rows": rows_count,
            "delivery": "base64_zip" if compress else "inline",
            "note": "Remote MCP servers cannot write into ChatGPT /mnt/data directly; decode the returned bundle if a physical file is needed.",
        }
        files["manifest.json"] = _json_dump(manifest)

        if compress:
            bundle, size, sha256 = _zip_files(files)
            return ResponseBuilder.build_response(
                data={
                    "activity_id": activity_id,
                    "bundle_format": "zip_base64",
                    "bundle_base64": bundle,
                    "files": list(files.keys()),
                    "rows": rows_count,
                    "size_bytes": size,
                    "sha256": sha256,
                    "manifest": manifest,
                },
                query_type="activity_data_export",
            )

        text_files = {
            name: content.decode("latin1") if isinstance(content, bytes) else content
            for name, content in files.items()
        }
        return ResponseBuilder.build_response(
            data={"activity_id": activity_id, "files": text_files, "rows": rows_count, "manifest": manifest},
            query_type="activity_data_export",
        )
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(f"Invalid include_json: {str(e)}", error_type="validation_error")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )


async def get_activity_streams_table(
    activity_id: Annotated[str, "Activity ID to convert into a compact second-by-second table"],
    max_rows: Annotated[int, "Maximum rows to return. Rows are evenly downsampled if needed."] = 500,
    ctx: Context | None = None,
) -> str:
    """Return a compact table derived from activity streams for external analysis."""
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            streams_data = await client.get_activity_streams(activity_id)
        rows = _build_rows(streams_data)
        total = len(rows)
        if max_rows and total > max_rows:
            step = max(total // max_rows, 1)
            rows = rows[::step][:max_rows]
        return ResponseBuilder.build_response(
            data={
                "activity_id": activity_id,
                "total_rows": total,
                "returned_rows": len(rows),
                "available_streams": list(_all_streams(streams_data).keys()),
                "stream_lengths": {
                    name: len(value)
                    for name, value in _all_streams(streams_data).items()
                    if isinstance(value, list)
                },
                "rows": rows,
            },
            query_type="activity_streams_table",
        )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )
