#!/usr/bin/env python3
"""Safely recycle running Vast.ai Gigahash instances onto the latest image.

The script is intentionally self-contained and uses only Python's standard
library plus an installed and authenticated ``vastai`` command-line client.
It performs a dry run unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence, TextIO
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_IMAGE = "ghcr.io/madmax43v3r/gigahash:latest"
DEFAULT_POOL_API_URL = "https://gigahash.cloud"
DEFAULT_READY_TIMEOUT_SECONDS = 10_800
DEFAULT_POLL_SECONDS = 5
DEFAULT_POOL_TIMEOUT_SECONDS = 15
BASE58_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{40,80}$")


class UpdateError(RuntimeError):
    """An expected, user-actionable update failure."""


class SkipInstance(UpdateError):
    """The instance became ineligible before a recycle was requested."""


@dataclass(frozen=True)
class RecycleConfiguration:
    image: str
    image_runtype: str
    image_arguments: tuple[str, ...]
    environment: dict[str, Any]
    onstart: str


@dataclass(frozen=True)
class WorkerConfiguration:
    payout: str
    worker_name: str
    expected_gpus: int
    available_gpus: int


def positive_integer(value: str, name: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise UpdateError(f"{name} must be a positive integer")
    return int(value)


def normalize_environment(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise UpdateError("Vast extra_env contains a non-string key")
        return dict(value)
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for entry in value:
            if (
                not isinstance(entry, (list, tuple))
                or len(entry) != 2
                or not isinstance(entry[0], str)
            ):
                raise UpdateError("Vast extra_env is not an object or key/value list")
            key, entry_value = entry
            if key in result:
                raise UpdateError(f"Vast extra_env contains duplicate key {key!r}")
            result[key] = entry_value
        return result
    raise UpdateError("Vast extra_env is not an object or key/value list")


def vast_jupyter_image(image: str) -> str:
    if ":" not in image:
        raise UpdateError("Jupyter recycling requires a tagged image")
    repository, tag = image.rsplit(":", 1)
    if not repository or not tag:
        raise UpdateError("Jupyter recycling requires a tagged image")
    return f"{repository}_{tag}/jupyter"


def parse_api_error(stderr: str) -> str | None:
    for line in reversed(stderr.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("error"):
            continue
        status = payload.get("status_code")
        message = payload.get("msg") or payload.get("message") or payload
        return f"HTTP {status}: {message}" if isinstance(status, int) else str(message)
    return None


def validate_recycle_output(output: str, instance_id: int) -> None:
    text = output.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        if payload.get("success") is True:
            return
        raise UpdateError(f"Vast recycle failed: {payload!r}")
    if text == f"Recycling instance {instance_id}.":
        # Vast CLI 1.4.2 ignores --raw for recycle and emits this on success.
        return
    raise UpdateError(f"unexpected Vast recycle response: {text[:500]!r}")


class VastClient:
    def __init__(self, executable: str) -> None:
        self.executable = executable

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *arguments]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as error:
            raise UpdateError(f"Vast CLI not found: {self.executable}") from error
        except subprocess.TimeoutExpired as error:
            raise UpdateError(f"Vast command timed out: {' '.join(command[:4])}") from error
        api_error = parse_api_error(completed.stderr)
        if api_error is not None:
            raise UpdateError(f"Vast command failed: {api_error}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise UpdateError(
                f"Vast command {' '.join(command[:4])} failed: {detail[:500]}"
            )
        return completed

    def _run_json(self, arguments: Sequence[str]) -> Any:
        completed = self._run(arguments)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise UpdateError(
                f"Vast returned invalid JSON for {' '.join(arguments[:3])}: "
                f"{completed.stdout.strip()[:500]!r}"
            ) from error

    def show_instance(self, instance_id: int) -> dict[str, Any]:
        payload = self._run_json(["show", "instance", str(instance_id), "--raw"])
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise UpdateError(f"could not read Vast instance {instance_id}: {payload!r}")
        if payload.get("id") != instance_id:
            raise UpdateError(
                f"Vast returned instance {payload.get('id')!r} when {instance_id} was requested"
            )
        return payload

    def instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        next_token = ""
        while True:
            arguments = ["show", "instances-v1", "--limit", "25", "--raw"]
            if next_token:
                arguments.extend(["--next-token", next_token])
            payload = self._run_json(arguments)
            rows = payload.get("instances") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict)
                or not isinstance(rows, list)
                or payload.get("success") is not True
            ):
                raise UpdateError("Vast instances-v1 response has no instances array")
            instances.extend(row for row in rows if isinstance(row, dict))
            next_token = str(payload.get("next_token") or "")
            if not next_token:
                return instances

    def recycle_instance(self, instance_id: int) -> None:
        completed = self._run(["recycle", "instance", str(instance_id), "--raw"])
        validate_recycle_output(completed.stdout, instance_id)


class PoolClient:
    def __init__(self, base_url: str, payout: str, timeout: int) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UpdateError("pool API URL must be an absolute HTTP or HTTPS URL")
        self.url = (
            f"{base_url.rstrip('/')}/api/v1/miners/"
            f"{urllib.parse.quote(payout, safe='')}"
        )
        self.timeout = timeout

    def account(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url, headers={"User-Agent": "gigahash-vast-update/1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise UpdateError(f"pool API returned HTTP {response.status}")
                data = response.read(4 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError) as error:
            raise UpdateError(f"pool API request failed: {error}") from error
        if len(data) > 4 * 1024 * 1024:
            raise UpdateError("pool API response is too large")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise UpdateError(f"pool API returned invalid JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("workers"), list):
            raise UpdateError("pool API response has no workers array")
        return payload


def previous_worker_ids(account: Mapping[str, Any], worker_name: str) -> set[str]:
    result: set[str] = set()
    for worker in account.get("workers", []):
        if not isinstance(worker, dict) or str(worker.get("worker_name") or "") != worker_name:
            continue
        worker_id = worker.get("worker_instance_id")
        if isinstance(worker_id, str) and worker_id:
            result.add(worker_id)
    return result


def worker_readiness(
    account: Mapping[str, Any],
    worker_name: str,
    expected_gpus: int,
    earliest_last_seen: int,
    old_worker_ids: set[str],
    expected_miner_version: str | None = None,
) -> tuple[bool, str]:
    named_workers = [
        worker
        for worker in account.get("workers", [])
        if isinstance(worker, dict)
        and str(worker.get("worker_name") or "") == worker_name
    ]
    if not named_workers:
        return False, "worker has not appeared in pool telemetry"
    for worker in named_workers:
        worker_id = worker.get("worker_instance_id")
        last_seen = worker.get("last_seen")
        rate = worker.get("rate_milli_units_per_second")
        if not bool(worker.get("online")):
            continue
        if not isinstance(worker_id, str) or not worker_id or worker_id in old_worker_ids:
            continue
        if not isinstance(last_seen, (int, float)) or last_seen < earliest_last_seen:
            continue
        if not isinstance(rate, (int, float)) or rate <= 0:
            continue
        miner_version = str(worker.get("miner_version") or "")
        if expected_miner_version is not None and miner_version != expected_miner_version:
            continue
        online_devices = {
            gpu.get("device_index")
            for gpu in worker.get("gpus", [])
            if isinstance(gpu, dict)
            and bool(gpu.get("online"))
            and isinstance(gpu.get("device_index"), int)
        }
        if len(online_devices) != expected_gpus:
            continue
        return (
            True,
            f"active {worker.get('puzzle', 'unknown')} worker {worker_id} "
            f"with {len(online_devices)}/{expected_gpus} GPUs online"
            + (f" on version {miner_version}" if miner_version else ""),
        )
    version_requirement = (
        f" on version {expected_miner_version}"
        if expected_miner_version is not None
        else ""
    )
    return (
        False,
        "waiting for a new worker with all GPUs online, positive rate"
        f"{version_requirement}",
    )


def expected_online_gpus(environment: Mapping[str, Any], available_gpus: int) -> int:
    device = str(environment.get("DEVICE") or "")
    devices = str(environment.get("DEVICES") or "")
    if device and devices:
        raise UpdateError("preserved DEVICE and DEVICES settings are mutually exclusive")
    selected = device or devices
    if not selected:
        return available_gpus
    parts = selected.split(",")
    if any(not part.isdigit() for part in parts):
        raise UpdateError("preserved DEVICE/DEVICES setting is not a numeric device list")
    indexes = {int(part) for part in parts}
    if len(indexes) != len(parts) or any(index >= available_gpus for index in indexes):
        raise UpdateError("preserved DEVICE/DEVICES setting has duplicate or unavailable GPUs")
    if device and len(indexes) != 1:
        raise UpdateError("preserved DEVICE setting must select exactly one GPU")
    return len(indexes)


def tagged_images(image: str) -> frozenset[str]:
    return frozenset({image.lower(), vast_jupyter_image(image).lower()})


def image_is_selected(instance: Mapping[str, Any], images: frozenset[str]) -> bool:
    return str(instance.get("image_uuid") or "").strip().lower() in images


def selected_instances(
    instances: Sequence[Mapping[str, Any]], images: frozenset[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    running: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for raw in instances:
        if not image_is_selected(raw, images):
            continue
        instance_id = raw.get("id")
        if not isinstance(instance_id, int) or isinstance(instance_id, bool):
            raise UpdateError(f"Gigahash inventory row has invalid ID: {instance_id!r}")
        if instance_id in seen_ids:
            raise UpdateError(f"Vast inventory contains duplicate instance ID {instance_id}")
        seen_ids.add(instance_id)
        instance = dict(raw)
        if (
            (instance.get("intended_status") or "") == "running"
            and (instance.get("actual_status") or "") == "running"
        ):
            running.append(instance)
        else:
            inactive.append(instance)
    running.sort(key=lambda row: int(row["id"]))
    inactive.sort(key=lambda row: int(row["id"]))
    return running, inactive


def normalized_arguments(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(argument) for argument in value)
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError as error:
            raise UpdateError(f"cannot parse Vast image_args {value!r}: {error}") from error
    raise UpdateError(f"Vast image_args has unsupported type {type(value).__name__}")


def runtime_arguments(instance: Mapping[str, Any]) -> tuple[str, ...]:
    runtype = str(instance.get("image_runtype") or "")
    if runtype == "args":
        return normalized_arguments(instance.get("image_args"))
    if "ssh" in runtype or "jupyter" in runtype:
        return normalized_arguments(instance.get("onstart"))
    raise UpdateError(f"unsupported Gigahash image_runtype {runtype!r}")


def option_value(arguments: Sequence[str], option: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.startswith(f"{option}="):
            return argument.split("=", 1)[1]
        if argument == option:
            if index + 1 >= len(arguments):
                raise UpdateError(f"{option} has no value in the instance arguments")
            return arguments[index + 1]
    return None


def worker_configuration(instance: Mapping[str, Any], instance_id: int) -> WorkerConfiguration:
    environment = normalize_environment(instance.get("extra_env"))
    arguments = runtime_arguments(instance)
    payout_option = option_value(arguments, "--payout-address")
    payout = str(payout_option or environment.get("PAYOUT_ADDRESS") or "")
    if not BASE58_ADDRESS.fullmatch(payout):
        raise UpdateError(
            f"instance {instance_id} has no plausible payout address for readiness checks"
        )
    worker_option = option_value(arguments, "--worker-name")
    worker_name = str(worker_option or environment.get("WORKER_NAME") or instance_id)
    available_gpus = instance.get("num_gpus")
    if (
        not isinstance(available_gpus, int)
        or isinstance(available_gpus, bool)
        or available_gpus <= 0
    ):
        raise UpdateError(f"instance {instance_id} has invalid num_gpus")
    effective_environment = dict(environment)
    environment_device = str(environment.get("DEVICE") or "")
    environment_devices = str(environment.get("DEVICES") or "")
    if environment_device and environment_devices:
        raise UpdateError("preserved DEVICE and DEVICES settings are mutually exclusive")
    argument_device = option_value(arguments, "--device")
    argument_devices = option_value(arguments, "--devices")
    device = argument_device if argument_device is not None else environment_device
    devices = argument_devices if argument_devices is not None else environment_devices
    if device and devices:
        raise UpdateError("effective --device and --devices settings are mutually exclusive")
    effective_environment["DEVICE"] = device
    effective_environment["DEVICES"] = devices
    expected_gpus = expected_online_gpus(effective_environment, available_gpus)
    return WorkerConfiguration(payout, worker_name, expected_gpus, available_gpus)


def capture_configuration(instance: Mapping[str, Any]) -> RecycleConfiguration:
    return RecycleConfiguration(
        image=str(instance.get("image_uuid") or ""),
        image_runtype=str(instance.get("image_runtype") or ""),
        image_arguments=normalized_arguments(instance.get("image_args")),
        environment=normalize_environment(instance.get("extra_env")),
        onstart=str(instance.get("onstart") or ""),
    )


def configuration_matches(
    instance: Mapping[str, Any],
    expected: RecycleConfiguration,
    accepted_images: frozenset[str],
) -> bool:
    try:
        current = capture_configuration(instance)
    except UpdateError:
        return False
    return (
        current.image.strip().lower() in accepted_images
        and current.image_runtype == expected.image_runtype
        and current.image_arguments == expected.image_arguments
        and current.environment == expected.environment
        and current.onstart == expected.onstart
    )


def wait_for_recycled_worker(
    vast: VastClient,
    pool: PoolClient,
    instance_id: int,
    worker: WorkerConfiguration,
    expected_configuration: RecycleConfiguration,
    accepted_images: frozenset[str],
    recycle_started: int,
    old_worker_ids: set[str],
    timeout: int,
    poll_seconds: int,
    expected_miner_version: str | None,
) -> str:
    deadline = time.monotonic() + timeout
    last_detail = ""
    while True:
        details: list[str] = []
        try:
            instance = vast.show_instance(instance_id)
        except UpdateError as error:
            details.append(str(error))
        else:
            if (instance.get("intended_status") or "") != "running":
                raise UpdateError(
                    f"instance intended status changed to {instance.get('intended_status')!r}"
                )
            if not configuration_matches(instance, expected_configuration, accepted_images):
                raise UpdateError("instance configuration changed during recycle")
            if (instance.get("actual_status") or "") != "running":
                details.append(f"Vast status is {instance.get('actual_status') or 'unknown'}")

        try:
            ready, pool_detail = worker_readiness(
                pool.account(),
                worker.worker_name,
                worker.expected_gpus,
                recycle_started,
                old_worker_ids,
                expected_miner_version,
            )
            if not ready:
                details.append(pool_detail)
        except UpdateError as error:
            ready = False
            pool_detail = str(error)
            details.append(pool_detail)

        if not details and ready:
            return pool_detail
        detail = "; ".join(details)
        if time.monotonic() >= deadline:
            raise UpdateError(
                f"instance {instance_id} did not resume verified mining within {timeout}s: "
                f"{detail}"
            )
        if detail != last_detail:
            print(f"Waiting for instance {instance_id}: {detail}", flush=True)
            last_detail = detail
        time.sleep(poll_seconds)


def recycle_one(
    vast: VastClient,
    instance_id: int,
    images: frozenset[str],
    pool_api_url: str,
    pool_timeout: int,
    ready_timeout: int,
    poll_seconds: int,
    expected_miner_version: str | None,
) -> str:
    instance = vast.show_instance(instance_id)
    if not image_is_selected(instance, images):
        raise SkipInstance("instance no longer uses the selected Gigahash image")
    if (instance.get("intended_status") or "") != "running":
        raise SkipInstance(
            f"instance is no longer intended to run: {instance.get('intended_status')!r}"
        )
    if (instance.get("actual_status") or "") != "running":
        raise SkipInstance(
            f"instance is no longer running: {instance.get('actual_status')!r}"
        )

    expected_configuration = capture_configuration(instance)
    worker = worker_configuration(instance, instance_id)
    pool = PoolClient(pool_api_url, worker.payout, pool_timeout)
    try:
        old_worker_ids = previous_worker_ids(pool.account(), worker.worker_name)
    except UpdateError as error:
        raise UpdateError(
            f"cannot establish a pool readiness baseline; refusing to recycle: {error}"
        ) from error

    recycle_started = int(time.time())
    vast.recycle_instance(instance_id)
    return wait_for_recycled_worker(
        vast,
        pool,
        instance_id,
        worker,
        expected_configuration,
        images,
        recycle_started,
        old_worker_ids,
        ready_timeout,
        poll_seconds,
        expected_miner_version,
    )


def acquire_lock(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise UpdateError(f"another Gigahash update run holds {path}") from error
    return handle


def display_mode(instance: Mapping[str, Any]) -> str:
    arguments = normalized_arguments(instance.get("image_args"))
    return arguments[0] if arguments else "onstart"


def instance_id(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("instance ID must be a positive integer")
    return int(value)


def positive_argument(value: str) -> int:
    try:
        return positive_integer(value, "value")
    except UpdateError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def default_lock_path() -> Path:
    return Path.home() / ".cache" / "gigahash" / "vastai-update.lock"


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially recycle running Vast.ai instances already configured "
            "with the selected Gigahash image. Dry-run is the default."
        )
    )
    parser.add_argument("--apply", action="store_true", help="perform rolling recycles")
    parser.add_argument(
        "--instance",
        action="append",
        type=instance_id,
        default=[],
        metavar="ID",
        help="recycle only this Vast instance (repeatable)",
    )
    parser.add_argument(
        "--expect-version",
        metavar="VERSION",
        help="require each replacement worker to report this miner version",
    )
    parser.add_argument(
        "--image",
        default=os.environ.get("GIGAHASH_IMAGE", DEFAULT_IMAGE),
        help=f"image tag to recycle (default: {DEFAULT_IMAGE})",
    )
    parser.add_argument(
        "--pool-api-url",
        default=os.environ.get("POOL_API_URL", DEFAULT_POOL_API_URL),
        help=f"pool API used for readiness checks (default: {DEFAULT_POOL_API_URL})",
    )
    parser.add_argument(
        "--vastai-bin",
        default=os.environ.get("VASTAI_BIN", "vastai"),
        help="Vast.ai CLI executable (default: vastai)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=positive_argument,
        default=positive_integer(
            os.environ.get(
                "VAST_READY_TIMEOUT_SECONDS", str(DEFAULT_READY_TIMEOUT_SECONDS)
            ),
            "VAST_READY_TIMEOUT_SECONDS",
        ),
        metavar="SECONDS",
        help=f"maximum wait per instance (default: {DEFAULT_READY_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--poll-seconds",
        type=positive_argument,
        default=positive_integer(
            os.environ.get("VAST_RECYCLE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)),
            "VAST_RECYCLE_POLL_SECONDS",
        ),
        metavar="SECONDS",
        help=f"readiness polling interval (default: {DEFAULT_POLL_SECONDS})",
    )
    parser.add_argument(
        "--pool-timeout",
        type=positive_argument,
        default=positive_integer(
            os.environ.get(
                "POOL_API_TIMEOUT_SECONDS", str(DEFAULT_POOL_TIMEOUT_SECONDS)
            ),
            "POOL_API_TIMEOUT_SECONDS",
        ),
        metavar="SECONDS",
        help=f"pool API request timeout (default: {DEFAULT_POOL_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path(os.environ.get("VAST_RECYCLE_LOCK", default_lock_path())),
        metavar="PATH",
        help=f"exclusive-run lock (default: {default_lock_path()})",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str]) -> int:
    arguments = parse_arguments(argv)
    image = arguments.image.strip()
    if not image or image != arguments.image or "\n" in image or "\r" in image:
        raise UpdateError("image must be a non-empty single-line value without surrounding spaces")
    images = tagged_images(image)
    vast = VastClient(arguments.vastai_bin)
    inventory = vast.instances()
    if arguments.instance:
        requested_ids = set(arguments.instance)
        inventory_by_id = {
            row.get("id"): row
            for row in inventory
            if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
        }
        missing_ids = sorted(requested_ids - inventory_by_id.keys())
        if missing_ids:
            raise UpdateError(
                "Vast inventory does not contain requested instance(s): "
                + ", ".join(str(value) for value in missing_ids)
            )
        inventory = [inventory_by_id[value] for value in sorted(requested_ids)]
        wrong_image_ids = [
            int(row["id"]) for row in inventory if not image_is_selected(row, images)
        ]
        if wrong_image_ids:
            raise UpdateError(
                "requested instance(s) do not use the selected Gigahash image: "
                + ", ".join(str(value) for value in wrong_image_ids)
            )
    running, inactive = selected_instances(inventory, images)

    print("Running Gigahash instances selected for sequential recycle:")
    print(f"{'ID':<10} {'GPUS':<5} {'MODE':<10} {'RUNTIME':<32} LABEL")
    for instance in running:
        print(
            f"{instance['id']:<10} {instance.get('num_gpus', '-')!s:<5} "
            f"{display_mode(instance):<10} "
            f"{str(instance.get('image_runtype') or '-'):<32} "
            f"{instance.get('label') or '-'}"
        )
    print(f"Selected: {len(running)} running; skipped inactive: {len(inactive)}")
    print(f"Image tag: {image}")
    if arguments.expect_version:
        print(f"Required replacement version: {arguments.expect_version}")

    if not running:
        print("No running Gigahash instances need recycling.")
        return 0
    if not arguments.apply:
        print("Dry run only. Re-run with --apply to recycle these instances one at a time.")
        return 0

    lock_handle = acquire_lock(arguments.lock_file)
    try:
        completed = 0
        skipped = 0
        for index, instance in enumerate(running, start=1):
            selected_id = int(instance["id"])
            print(
                f"[{index}/{len(running)}] Recycling instance {selected_id}...",
                flush=True,
            )
            try:
                detail = recycle_one(
                    vast,
                    selected_id,
                    images,
                    arguments.pool_api_url,
                    arguments.pool_timeout,
                    arguments.ready_timeout,
                    arguments.poll_seconds,
                    arguments.expect_version,
                )
            except SkipInstance as error:
                skipped += 1
                print(
                    f"[{index}/{len(running)}] Skipped instance {selected_id} before "
                    f"recycle: {error}."
                )
                continue
            except UpdateError as error:
                remaining = len(running) - index
                raise UpdateError(
                    f"instance {selected_id} failed; stopped with {remaining} later "
                    f"instances untouched: {error}"
                ) from error
            completed += 1
            print(f"[{index}/{len(running)}] Instance {selected_id} ready: {detail}.")
    finally:
        lock_handle.close()
    print(
        f"Update complete: {completed} Gigahash instances are verified online; "
        f"{skipped} became ineligible and were not touched."
    )
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except UpdateError as error:
        print(f"update failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("update interrupted; no later instances were touched", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
