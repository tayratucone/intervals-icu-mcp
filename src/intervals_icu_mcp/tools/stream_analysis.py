"""Derived analysis from Intervals.icu activity streams."""

import json
from statistics import mean
from typing import Annotated, Any

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
    for i in range(length):
        speed_value = _safe(speed[i]) if i < len(speed) else None
        pace = _pace_from_speed(speed_value)
        rows.append(
            {
                "second": int(time[i]) if i < len(time) and isinstance(time[i], (int, float)) else i,
                "distance_m": round(float(distance[i]), 1)
                if i < len(distance) and isinstance(distance[i], (int, float))
                else None,
                "pace_sec_per_km": round(pace, 1) if pace else None,
                "pace": _format_pace(pace),
                "hr": int(hr[i]) if i < len(hr) and isinstance(hr[i], (int, float)) else None,
                "cadence": round(float(cadence[i]), 1)
                if i < len(cadence) and isinstance(cadence[i], (int, float))
                else None,
                "watts": int(power[i]) if i < len(power) and isinstance(power[i], (int, float)) else None,
                "speed_mps": round(speed_value, 3) if speed_value is not None else None,
                "altitude_m": round(float(altitude[i]), 1)
                if i < len(altitude) and isinstance(altitude[i], (int, float))
                else None,
                "grade_pct": round(float(grade[i]), 1)
                if i < len(grade) and isinstance(grade[i], (int, float))
                else None,
                "moving": bool(moving[i]) if i < len(moving) and moving[i] is not None else None,
            }
        )
        for stream_name, stream_values in list_streams.items():
            if stream_name in rows[-1]:
                continue
            if len(stream_values) == length and i < len(stream_values):
                rows[-1][stream_name] = stream_values[i]
    return rows


def _moving_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("moving") is not False and row.get("speed_mps")]


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
