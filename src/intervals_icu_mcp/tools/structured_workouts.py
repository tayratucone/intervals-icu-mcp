"""Structured workout creation for Intervals.icu calendar events."""

import json
import re
from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..date_utils import parse_date, start_datetime
from ..response_builder import ResponseBuilder


SPORT_TYPES = {
    "bike": "Ride",
    "biking": "Ride",
    "cycling": "Ride",
    "cycle": "Ride",
    "velo": "Ride",
    "vélo": "Ride",
    "ride": "Ride",
    "run": "Run",
    "running": "Run",
    "course": "Run",
    "course_a_pied": "Run",
    "course à pied": "Run",
    "trail": "Run",
    "trail_run": "Run",
    "swim": "Swim",
    "swimming": "Swim",
    "natation": "Swim",
    "strength": "WeightTraining",
    "musculation": "WeightTraining",
    "weights": "WeightTraining",
}

STEP_LABELS = {
    "echauffement": "Warmup",
    "warmup": "Warmup",
    "endurance": "Endurance",
    "tempo": "Tempo",
    "threshold": "Threshold",
    "seuil": "Threshold",
    "technique": "Technique",
    "drill": "Drill",
    "recuperation": "Recovery",
    "recovery": "Recovery",
    "rest": "Recovery",
    "retour_au_calme": "Cooldown",
    "cooldown": "Cooldown",
    "cool_down": "Cooldown",
    "work": "Work",
}


def _parse_payload(workout: str | dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(workout, str):
        parsed = json.loads(workout)
    else:
        parsed = workout

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return parsed
    raise ValueError("Workout payload must be a JSON object or an array of objects")


def _sport_type(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Run"
    return SPORT_TYPES.get(text.lower(), text)


def _duration_to_icu(value: Any) -> str:
    if value is None:
        raise ValueError("Missing duration")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            seconds = int(float(text))
        else:
            parts = text.split(":")
            if len(parts) == 2:
                seconds = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                return text

    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    result = ""
    if hours:
        result += f"{hours}h"
    if minutes:
        result += f"{minutes}m"
    if secs or not result:
        result += f"{secs}s"
    return result


def _distance_to_icu(value: Any) -> str:
    meters = float(value)
    if meters >= 1000 and meters % 1000 == 0:
        return f"{int(meters / 1000)}km"
    if meters >= 1000:
        return f"{meters / 1000:.3f}".rstrip("0").rstrip(".") + "km"
    return f"{int(meters)}mtr"


def _pace_to_seconds(value: Any, unit: str) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) == 2:
        seconds = int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    else:
        seconds = int(float(text))
    if unit.lower().replace(" ", "") in {"min/100m", "/100m", "sec/100m"}:
        return seconds
    return seconds


def _seconds_to_pace(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _target_to_icu(target: Any, sport: str) -> tuple[str, str | None]:
    if not isinstance(target, dict):
        return "", None

    target_type = str(target.get("type") or "none").lower()
    if target_type in {"none", "open", ""}:
        return "", None

    unit = str(target.get("unit") or "").strip()
    min_value = target.get("min", target.get("min_sec_per_km", target.get("min_sec_per_100m")))
    max_value = target.get("max", target.get("max_sec_per_km", target.get("max_sec_per_100m")))
    value = target.get("value")

    if target_type in {"pace", "swim_pace", "allure"}:
        if target.get("min_sec_per_100m") is not None or "100m" in unit.lower():
            suffix = "/100m Pace"
            low = _pace_to_seconds(min_value if min_value is not None else value, "min/100m")
            high = _pace_to_seconds(max_value if max_value is not None else value, "min/100m")
        else:
            suffix = "/km Pace"
            low = _pace_to_seconds(min_value if min_value is not None else value, "min/km")
            high = _pace_to_seconds(max_value if max_value is not None else value, "min/km")
        if min_value is not None and max_value is not None and low != high:
            return f"{_seconds_to_pace(low)}-{_seconds_to_pace(high)}{suffix}", "PACE"
        return f"{_seconds_to_pace(low)}{suffix}", "PACE"

    if target_type in {"hr", "heart_rate", "heartrate", "fc"}:
        if str(value or "").upper().startswith("Z"):
            return f"{str(value).upper()} HR", "HR"
        if min_value is not None and max_value is not None:
            return f"{int(min_value)}-{int(max_value)} HR", "HR"
        if value is not None:
            return f"{int(value)} HR", "HR"
        zone = target.get("zone")
        if zone is not None:
            return f"Z{zone} HR", "HR"

    if target_type in {"power", "puissance", "watts"}:
        if str(value or "").upper().startswith("Z"):
            return f"{str(value).upper()} Power", "POWER"
        if unit in {"%", "percent", "%ftp"}:
            if min_value is not None and max_value is not None:
                return f"{int(min_value)}-{int(max_value)}%", "POWER"
            if value is not None:
                return f"{int(value)}%", "POWER"
        if min_value is not None and max_value is not None:
            return f"{int(min_value)}-{int(max_value)}w", "POWER"
        if value is not None:
            return f"{int(value)}w", "POWER"
        zone = target.get("zone")
        if zone is not None:
            return f"Z{zone} Power", "POWER"

    if target_type in {"zone", "garmin_zone"}:
        zone = str(value or target.get("zone") or "").upper()
        metric = str(target.get("metric") or "").lower()
        if metric in {"hr", "heart_rate", "fc"}:
            return f"{zone if zone.startswith('Z') else 'Z' + zone} HR", "HR"
        if metric in {"pace", "allure"}:
            return f"{zone if zone.startswith('Z') else 'Z' + zone} Pace", "PACE"
        if metric in {"power", "puissance"}:
            return f"{zone if zone.startswith('Z') else 'Z' + zone} Power", "POWER"
        if sport == "Ride":
            return f"{zone if zone.startswith('Z') else 'Z' + zone} Power", "POWER"
        return f"{zone if zone.startswith('Z') else 'Z' + zone} Pace", "PACE"

    return "", None


def _step_label(step: dict[str, Any]) -> str:
    raw = str(step.get("type") or step.get("kind") or "").strip()
    return STEP_LABELS.get(raw.lower(), raw.replace("_", " ").title()) if raw else ""


def _step_line(step: dict[str, Any], sport: str) -> tuple[str, str | None, int, float]:
    if step.get("distance") is not None:
        duration_or_distance = _distance_to_icu(step["distance"])
        distance = float(step["distance"])
        duration = 0
    elif step.get("distance_meters") is not None:
        duration_or_distance = _distance_to_icu(step["distance_meters"])
        distance = float(step["distance_meters"])
        duration = 0
    else:
        duration = _duration_seconds(step.get("duration_seconds", step.get("duration")))
        duration_or_distance = _duration_to_icu(duration)
        distance = 0.0

    target_text, target_metric = _target_to_icu(step.get("target"), sport)
    cadence = step.get("cadence") or step.get("cadence_rpm")
    cadence_text = f" {int(cadence)}rpm" if cadence else ""
    label = _step_label(step)

    parts = ["-"]
    if label:
        parts.append(label)
    parts.append(duration_or_distance)
    if target_text:
        parts.append(target_text)
    line = " ".join(parts) + cadence_text

    comment_parts = []
    if target_text:
        comment_parts.append(f"Objectif: {target_text}")
    if step.get("description"):
        comment_parts.append(str(step["description"]).strip())
    if comment_parts:
        line += " | " + " ; ".join(comment_parts)

    return line, target_metric, duration, distance


def _duration_seconds(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return int(float(text))
    parts = text.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
    if match:
        return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(
            match.group(3) or 0
        )
    return 0


def _render_steps(
    steps: list[dict[str, Any]], sport: str, depth: int = 0
) -> tuple[list[str], list[str], int, float]:
    lines: list[str] = []
    target_metrics: list[str] = []
    total_duration = 0
    total_distance = 0.0

    for step in steps:
        repeat = int(step.get("repeat") or 0)
        nested = step.get("steps")
        if repeat and isinstance(nested, list):
            title = str(step.get("name") or step.get("type") or "Repeat").replace("_", " ").title()
            lines.append(f"{title} x{repeat}")
            nested_lines, nested_targets, nested_duration, nested_distance = _render_steps(
                nested, sport, depth + 1
            )
            lines.extend(nested_lines)
            lines.append("")
            target_metrics.extend(nested_targets * repeat)
            total_duration += nested_duration * repeat
            total_distance += nested_distance * repeat
            continue

        line, target_metric, duration, distance = _step_line(step, sport)
        lines.append(line)
        if target_metric:
            target_metrics.append(target_metric)
        total_duration += duration
        total_distance += distance

    return lines, target_metrics, total_duration, total_distance


def _global_target(metrics: list[str]) -> str | None:
    unique = {metric for metric in metrics if metric}
    if len(unique) == 1:
        return next(iter(unique))
    return None


def _workout_to_event(workout: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    date = workout.get("date") or workout.get("start_date") or workout.get("start_date_local")
    if not date:
        raise ValueError("Workout is missing date/start_date_local")
    parse_date(str(date))

    sport = _sport_type(workout.get("sport") or workout.get("type"))
    steps = workout.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Workout is missing a non-empty steps array")

    lines, target_metrics, total_duration, total_distance = _render_steps(steps, sport)
    builder_description = "\n".join(line for line in lines if line is not None).strip()
    intro = str(workout.get("description") or "").strip()
    description = f"{intro}\n\n{builder_description}" if intro else builder_description

    event_data: dict[str, Any] = {
        "category": "WORKOUT",
        "start_date_local": start_datetime(str(date)),
        "name": workout.get("name") or f"{sport} workout",
        "type": sport,
        "description": description,
    }
    if total_duration:
        event_data["moving_time"] = total_duration
    if total_distance:
        event_data["distance"] = total_distance
    if workout.get("training_load") is not None:
        event_data["icu_training_load"] = workout["training_load"]
    target = _global_target(target_metrics)
    if target:
        event_data["target"] = target

    preview = {
        "name": event_data["name"],
        "type": sport,
        "target": target,
        "description": description,
        "duration_seconds": total_duration or None,
        "distance_meters": total_distance or None,
    }
    return event_data, preview


async def create_structured_workout(
    workout: Annotated[
        str | dict[str, Any] | list[dict[str, Any]],
        "Structured workout JSON object or array. Fields: date, sport, name, description, steps. Steps may use duration/duration_seconds or distance/distance_meters, target, description, repeat + steps.",
    ],
    dry_run: Annotated[
        bool,
        "If true, only returns the generated Intervals.icu workout-builder text without creating events.",
    ] = False,
    ctx: Context | None = None,
) -> str:
    """Create one or more structured Intervals.icu planned workouts from JSON steps.

    The tool accepts a clean JSON structure for ChatGPT, converts it into the
    official Intervals.icu Workout Builder text syntax, and creates WORKOUT
    events on the calendar. This is the most reliable route supported by the
    Intervals.icu API for Garmin planned-workout sync.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        workouts = _parse_payload(workout)
        event_payloads: list[dict[str, Any]] = []
        previews: list[dict[str, Any]] = []
        for item in workouts:
            event_data, preview = _workout_to_event(item)
            event_payloads.append(event_data)
            previews.append(preview)

        if dry_run:
            return ResponseBuilder.build_response(
                data={"events": previews, "count": len(previews)},
                query_type="structured_workout_preview",
                metadata={"message": "Dry run only. No Intervals.icu events were created."},
            )

        created = []
        async with ICUClient(config) as client:
            for event_data in event_payloads:
                event = await client.create_event(event_data)
                created.append(
                    {
                        "id": event.id,
                        "start_date": event.start_date_local,
                        "name": event.name,
                        "category": event.category,
                        "type": event.type,
                        "target": event_data.get("target"),
                    }
                )

        return ResponseBuilder.build_response(
            data={"events": created, "generated": previews, "count": len(created)},
            query_type="create_structured_workout",
            metadata={"message": f"Successfully created {len(created)} structured workout(s)"},
        )

    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(
            f"Invalid JSON format: {str(e)}", error_type="validation_error"
        )
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {str(e)}", error_type="internal_error"
        )
