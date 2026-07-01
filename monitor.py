#!/usr/bin/env python3
"""Binance activity monitor for Debian/systemd.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Debian has fcntl; this keeps local parsing portable.
    fcntl = None  # type: ignore[assignment]

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ on supported Debian has zoneinfo.
    ZoneInfo = None  # type: ignore[assignment]


BANNER_URL = "https://www.binance.com/bapi/apex/v2/friendly/apex/marketing/banners"
RESOURCE_LIST_URL = (
    "https://www.binance.com/bapi/composite/v1/public/growth-paas/resource/list"
)
BINANCE_BASE_URL = "https://www.binance.com"

DEFAULT_STATE_PATH = "/var/lib/cf-binance-activity-monitor/state.json"
SEEN_LIMIT = 3000
MIN_RESOURCE_REQUEST_IDS = 5


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Binance activity entries.")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("MONITOR_ENV_FILE"),
        help="Optional KEY=VALUE env file to load before running.",
    )

    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="Run one monitor pass.")
    run_parser.add_argument("--dry-run", action="store_true", help="Do not write state or send Telegram alerts.")
    subparsers.add_parser("status", help="Print current state as JSON.")

    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    command = args.command or "run"
    if command == "status":
        print_json(load_state())
        return 0

    if command == "run":
        result = run_monitor(dry_run=args.dry_run)
        print_json(result)
        return 0

    parser.error(f"unknown command: {command}")
    return 2


def run_monitor(dry_run: bool) -> dict[str, Any]:
    state_path = pathlib.Path(get_env("STATE_PATH", DEFAULT_STATE_PATH))

    with state_lock(state_path):
        now = utc_now()
        state = load_state()
        first_run = not state.get("initializedAt")
        should_alert = (not first_run) or parse_bool(os.environ.get("ALERT_ON_FIRST_RUN"), False)
        alerts: list[dict[str, Any]] = []
        errors: list[str] = []

        try:
            banner_result = scan_banners(state, should_alert)
        except Exception as exc:  # noqa: BLE001 - errors are reported to Telegram.
            errors.append(error_message("banner", exc))
            banner_result = {"matches": 0, "alerts": []}

        resource_plan = plan_resource_scan(state)
        resource_result = scan_resources(state, resource_plan, should_alert)
        errors.extend(resource_result.get("errors", []))

        alerts.extend(banner_result.get("alerts", []))
        alerts.extend(resource_result.get("alerts", []))

        if not dry_run and alerts:
            send_telegram(format_alerts(alerts, now))
            state["lastAlertAt"] = now

        last_scan = {
            "at": now,
            "dryRun": dry_run,
            "firstRun": first_run,
            "resourceBatches": len(resource_plan["batches"]),
            "resourceIdsScanned": resource_plan["idsScanned"],
            "resourceMatches": resource_result["matches"],
            "bannerMatches": banner_result["matches"],
            "alerts": len(alerts) if should_alert else 0,
            "recentWindow": resource_plan.get("recentWindow"),
            "probeWindow": resource_plan.get("probeWindow"),
            "errors": errors,
        }

        if not dry_run:
            if not state.get("startupHeartbeatAt"):
                send_telegram(format_startup_heartbeat(first_run, last_scan, now))
                state["startupHeartbeatAt"] = now

            if errors:
                maybe_send_error_alert(state, errors, now)

            state["version"] = 1
            state.setdefault("initializedAt", now)
            state["runCount"] = int(state.get("runCount", 0)) + 1
            state["lastRunAt"] = now
            state["lastScan"] = last_scan
            state["maxObservedResourceId"] = max(
                int(state.get("maxObservedResourceId", resource_scan_start_id())),
                int(resource_result["maxSeenId"]),
            )
            state["nextProbeResourceId"] = resource_plan.get("nextProbeResourceId")
            prune_state(state)
            save_state(state)

        return {
            "ok": len(errors) == 0,
            "dryRun": dry_run,
            "firstRun": first_run,
            "alerts": alerts if should_alert else [],
            "suppressedFirstRunAlerts": 0 if should_alert else len(alerts),
            "lastScan": last_scan,
            "maxObservedResourceId": state.get("maxObservedResourceId"),
            "nextProbeResourceId": state.get("nextProbeResourceId"),
        }


def scan_banners(state: dict[str, Any], should_alert: bool) -> dict[str, Any]:
    payload = fetch_json(BANNER_URL, method="GET", headers=binance_headers())
    banners = extract_array(payload)
    alerts: list[dict[str, Any]] = []
    matches = 0

    for banner in banners:
        link = get_string(banner, ["link", "url", "webLink", "jumpUrl", "landingUrl"])
        if not link:
            continue

        matches += 1
        title = get_string(banner, ["title", "name", "bannerTitle"]) or "Binance banner"
        banner_id = get_string(banner, ["id", "bannerId", "resourceId"])
        key = f"id:{banner_id}" if banner_id else f"link:{link}"
        is_new = remember(state.setdefault("seenBannerKeys", []), key)

        if is_new and should_alert:
            alerts.append(
                {
                    "source": "banner",
                    "title": title,
                    "url": normalize_binance_url(link),
                    "details": compact(
                        [
                            f"id: {banner_id}" if banner_id else "",
                            format_time_detail("start", get_number(banner, ["startTime", "startAt"])),
                            format_time_detail("end", get_number(banner, ["endTime", "endAt"])),
                        ]
                    ),
                }
            )

    return {"matches": matches, "alerts": alerts}


def scan_resources(
    state: dict[str, Any], plan: dict[str, Any], should_alert: bool
) -> dict[str, Any]:
    alerts: list[dict[str, Any]] = []
    errors: list[str] = []
    matches = 0
    max_seen_id = int(state.get("maxObservedResourceId", resource_scan_start_id()))

    for ids in plan["batches"]:
        request_ids = pad_resource_request_ids(ids)
        try:
            payload = fetch_json(
                RESOURCE_LIST_URL,
                method="POST",
                headers={**binance_headers(), "content-type": "application/json"},
                body={"idList": request_ids, "pageIndex": 1, "pageSize": len(request_ids)},
            )
        except Exception as exc:  # noqa: BLE001 - continue other batches.
            errors.append(error_message(f"resource {ids[0]}-{ids[-1]}", exc))
            continue

        for resource in extract_array(payload):
            resource_id = get_number(resource, ["id", "resourceId"])
            if resource_id:
                max_seen_id = max(max_seen_id, int(resource_id))

            resource_type = get_string(resource, ["type"])
            code = get_string(resource, ["code"])
            uri = get_resource_uri(resource)
            if not uri or not uri.startswith("/activity/chance/"):
                continue

            matches += 1
            id_key = str(int(resource_id)) if resource_id else f"code:{code or uri}"
            code_key = code or uri
            is_new_id = remember(state.setdefault("seenResourceIds", []), id_key)
            remember(state.setdefault("seenResourceCodes", []), code_key)

            if is_new_id and should_alert:
                status = get_string(resource, ["status"])
                alerts.append(
                    {
                        "source": "resource",
                        "title": get_resource_title(resource) or code or uri,
                        "url": normalize_binance_url(uri),
                        "details": compact(
                            [
                                f"id: {int(resource_id)}" if resource_id else "",
                                f"code: {code}" if code else "",
                                f"type: {resource_type}" if resource_type else "",
                                f"status: {status}" if status else "",
                                format_time_detail(
                                    "published", get_number(resource, ["publishedTime"])
                                ),
                            ]
                        ),
                    }
                )

    return {"matches": matches, "alerts": alerts, "maxSeenId": max_seen_id, "errors": errors}


def plan_resource_scan(state: dict[str, Any]) -> dict[str, Any]:
    batch_size = clamp(read_int(os.environ.get("RESOURCE_BATCH_SIZE"), 100), 1, 100)
    max_batches = clamp(read_int(os.environ.get("RESOURCE_BATCHES_PER_RUN"), 35), 1, 40)
    probe_batches = clamp(
        read_int(os.environ.get("RESOURCE_PROBE_BATCHES_PER_RUN"), 10), 0, max_batches
    )
    recent_batches = max_batches - probe_batches
    start_id = resource_scan_start_id()
    backtrack = read_int(os.environ.get("RESOURCE_BACKTRACK"), 800)
    recent_ahead = read_int(os.environ.get("RESOURCE_RECENT_AHEAD"), 1700)
    max_observed = int(state.get("maxObservedResourceId") or start_id)

    batches: list[list[int]] = []
    recent_window = None
    probe_window = None

    if recent_batches > 0:
        recent_capacity = recent_batches * batch_size
        low = max(start_id, max_observed - backtrack)
        desired_high = max(low, max_observed + recent_ahead)
        high = min(desired_high, low + recent_capacity - 1)
        recent_window = {"low": low, "high": high}
        batches.extend(build_batches(low, high, batch_size))

    next_probe_resource_id = state.get("nextProbeResourceId")
    if probe_batches > 0:
        recent_high = recent_window["high"] if recent_window else start_id - 1
        probe_low = max(int(next_probe_resource_id or recent_high + 1), recent_high + 1)
        probe_high = probe_low + probe_batches * batch_size - 1
        probe_window = {"low": probe_low, "high": probe_high}
        next_probe_resource_id = probe_high + 1
        batches.extend(build_batches(probe_low, probe_high, batch_size))

    limited_batches = batches[:max_batches]
    return {
        "batches": limited_batches,
        "idsScanned": sum(len(batch) for batch in limited_batches),
        "recentWindow": recent_window,
        "probeWindow": probe_window,
        "nextProbeResourceId": next_probe_resource_id,
    }


def build_batches(low: int, high: int, batch_size: int) -> list[list[int]]:
    batches: list[list[int]] = []
    start = low
    while start <= high:
        end = min(high, start + batch_size - 1)
        batches.append(list(range(start, end + 1)))
        start += batch_size
    return batches


def pad_resource_request_ids(ids: list[int]) -> list[int]:
    if not ids or len(ids) >= MIN_RESOURCE_REQUEST_IDS:
        return ids

    padded = list(ids)
    previous = ids[0] - 1
    while len(padded) < MIN_RESOURCE_REQUEST_IDS and previous > 0:
        padded.insert(0, previous)
        previous -= 1

    next_id = ids[-1] + 1
    while len(padded) < MIN_RESOURCE_REQUEST_IDS:
        padded.append(next_id)
        next_id += 1

    return padded


def fetch_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> Any:
    timeout = read_int(os.environ.get("REQUEST_TIMEOUT_SECONDS"), 15)
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, "replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {text[:180]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-json response: {text[:180]}") from exc


def binance_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "clienttype": "android",
        "lang": binance_lang(),
        "user-agent": get_env("BINANCE_USER_AGENT", "Binance/3.16.7 Android"),
    }


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    for message in split_telegram_message(text):
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")
        if thread_id:
            body["message_thread_id"] = int(thread_id)

        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"telegram HTTP {exc.code}: {text[:180]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"telegram network error: {exc.reason}") from exc


def maybe_send_error_alert(state: dict[str, Any], errors: list[str], now: str) -> None:
    signature = " | ".join(errors)
    last_error_at = parse_iso_time(state.get("lastErrorAt"))
    cooldown_seconds = read_int(os.environ.get("ERROR_ALERT_COOLDOWN_SECONDS"), 3600)
    now_dt = parse_iso_time(now) or dt.datetime.now(dt.timezone.utc)
    should_send = (
        signature != state.get("lastErrorSignature")
        or last_error_at is None
        or (now_dt - last_error_at).total_seconds() > cooldown_seconds
    )

    state["lastErrorAt"] = now
    state["lastErrorSignature"] = signature

    if should_send:
        send_telegram(f"Binance activity monitor error\n{signature}")


def format_alerts(alerts: list[dict[str, Any]], now: str) -> str:
    lines = [f"Binance activity monitor found {len(alerts)} new item(s)", now, ""]
    for alert in alerts:
        lines.append(f"[{alert['source']}] {alert['title']}")
        lines.append(alert["url"])
        lines.extend(alert["details"])
        lines.append("")
    return "\n".join(lines).strip()


def format_startup_heartbeat(first_run: bool, last_scan: dict[str, Any], now: str) -> str:
    status = "ok" if not last_scan["errors"] else "error"
    lines = [
        "Binance activity monitor heartbeat",
        now,
        "",
        f"status: {status}",
        f"firstRun: {str(first_run).lower()}",
        f"bannerMatches: {last_scan['bannerMatches']}",
        f"resourceMatches: {last_scan['resourceMatches']}",
        f"resourceBatches: {last_scan['resourceBatches']}",
    ]
    if last_scan["errors"]:
        lines.append(f"errors: {len(last_scan['errors'])}")
    return "\n".join(lines)


def split_telegram_message(text: str) -> list[str]:
    limit = 3900
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < 1:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def load_state() -> dict[str, Any]:
    path = pathlib.Path(get_env("STATE_PATH", DEFAULT_STATE_PATH))
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if is_state(state):
                return state
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "version": 1,
        "runCount": 0,
        "maxObservedResourceId": resource_scan_start_id(),
        "seenResourceIds": [],
        "seenResourceCodes": [],
        "seenBannerKeys": [],
    }


def save_state(state: dict[str, Any]) -> None:
    path = pathlib.Path(get_env("STATE_PATH", DEFAULT_STATE_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def is_state(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("version") == 1
        and isinstance(value.get("seenResourceIds"), list)
        and isinstance(value.get("seenResourceCodes"), list)
        and isinstance(value.get("seenBannerKeys"), list)
    )


def prune_state(state: dict[str, Any]) -> None:
    state["seenResourceIds"] = list(state.get("seenResourceIds", []))[-SEEN_LIMIT:]
    state["seenResourceCodes"] = list(state.get("seenResourceCodes", []))[-SEEN_LIMIT:]
    state["seenBannerKeys"] = list(state.get("seenBannerKeys", []))[-SEEN_LIMIT:]


@contextlib.contextmanager
def state_lock(state_path: pathlib.Path):
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def remember(values: list[Any], value: str) -> bool:
    if value in values:
        return False
    values.append(value)
    return True


def extract_array(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    for key in ["list", "rows", "items", "resources", "banners", "data"]:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def get_resource_uri(resource: Any) -> str:
    if not isinstance(resource, dict):
        return ""
    global_content = resource.get("globalContent")
    if isinstance(global_content, dict) and isinstance(global_content.get("uri"), str):
        return global_content["uri"]
    return get_string(resource, ["uri", "url", "link"])


def get_resource_title(resource: Any) -> str:
    direct = get_string(resource, ["title", "name"])
    if direct:
        return direct
    if not isinstance(resource, dict):
        return ""

    for container_key in ["localizedContent", "globalContent", "content"]:
        title = find_nested_title(resource.get(container_key))
        if title:
            return title
    return ""


def find_nested_title(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ["title", "name", "activityTitle"]:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for child in value.values():
        nested = find_nested_title(child)
        if nested:
            return nested
    return ""


def get_string(value: Any, keys: list[str]) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, (int, float)):
            return str(candidate)
    return ""


def get_number(value: Any, keys: list[str]) -> float | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, str) and candidate:
            try:
                return float(candidate)
            except ValueError:
                pass
    return None


def normalize_binance_url(value: str) -> str:
    lang = binance_lang()
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        return f"{BINANCE_BASE_URL}/{value}"
    if value.startswith(f"/{lang}/"):
        return f"{BINANCE_BASE_URL}{value}"
    if value.startswith("/activity/"):
        return f"{BINANCE_BASE_URL}/{lang}{value}"
    return f"{BINANCE_BASE_URL}{value}"


def format_time_detail(label: str, timestamp: float | None) -> str:
    if not timestamp:
        return ""
    seconds = timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
    timezone = ZoneInfo("Asia/Hong_Kong") if ZoneInfo else dt.timezone(dt.timedelta(hours=8))
    try:
        date = dt.datetime.fromtimestamp(seconds, timezone)
    except (OSError, OverflowError, ValueError):
        return ""
    return f"{label}: {date:%Y-%m-%d %H:%M:%S} HKT"


def load_env_file(path: str) -> None:
    env_path = pathlib.Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resource_scan_start_id() -> int:
    return read_int(os.environ.get("RESOURCE_SCAN_START_ID"), 100003800)


def binance_lang() -> str:
    return os.environ.get("BINANCE_LANG") or os.environ.get("LANG") or "zh-CN"


def get_env(name: str, fallback: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def read_int(value: str | None, fallback: int) -> int:
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def clamp(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)


def parse_bool(value: str | None, fallback: bool) -> bool:
    if not value:
        return fallback
    return value.lower() in {"1", "true", "yes", "on"}


def parse_iso_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def compact(values: list[str]) -> list[str]:
    return [value for value in values if value]


def error_message(label: str, error: Exception) -> str:
    return f"{label}: {error}"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
