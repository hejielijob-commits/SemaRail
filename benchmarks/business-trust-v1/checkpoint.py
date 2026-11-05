"""Durable, credential-safe recovery support for business-trust-v1."""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


RECOVERY_PROTOCOL = {
    "schemaVersion": 1,
    "mode": "worker-ready-resume-v1",
    "secretProtection": "windows-dpapi-current-user",
    "deadlineClock": "unix-epoch-seconds",
    "recordCommit": "atomic-capture-prefix-v1",
}
CHECKPOINT_SCHEMA_VERSION = 1
LEDGER_NAME = "recovery-ledger.json"
SECRET_NAME = "private-secrets.dpapi"
ATTEMPTS_DIR = "recovery-attempts"
_FORBIDDEN_PUBLIC_KEY = re.compile(
    r"(?:api.?key|admin.?token|password|secret|credential|access.?token)", re.IGNORECASE
)


class CheckpointError(ValueError):
    """A checkpoint is unavailable, invalid, or cannot be safely recovered."""


def protocol_supported(manifest: Mapping[str, Any]) -> bool:
    value = manifest.get("recoveryProtocol")
    return (
        type(value) is dict
        and set(value) == set(RECOVERY_PROTOCOL)
        and type(value.get("schemaVersion")) is int
        and value == RECOVERY_PROTOCOL
    )


def require_protocol(manifest: Mapping[str, Any]) -> None:
    if not protocol_supported(manifest):
        raise CheckpointError("run does not declare the supported resume protocol")


def ledger_path(run_dir: Path) -> Path:
    return run_dir / ".recovery" / LEDGER_NAME


def secret_path(run_dir: Path) -> Path:
    return run_dir / ".recovery" / SECRET_NAME


def attempts_path(run_dir: Path) -> Path:
    return run_dir / ATTEMPTS_DIR


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _assert_public(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or _FORBIDDEN_PUBLIC_KEY.search(key):
                raise CheckpointError("public recovery evidence contains a credential field")
            _assert_public(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_public(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise CheckpointError("recovery evidence contains a non-finite number")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace a file atomically after the new bytes have reached disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, value: Any) -> None:
    _assert_public(value)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    atomic_write_bytes(path, encoded)


def write_json_new(path: Path, value: Any) -> None:
    """Create immutable local JSON evidence without exposing a partial file."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-directory hard link creates the destination atomically and
        # fails if it already exists on both NTFS and POSIX filesystems.
        os.link(temporary, path)
        temporary.unlink()
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_public_json_new(path: Path, value: Any) -> None:
    """Create immutable redacted recovery evidence."""
    _assert_public(value)
    write_json_new(path, value)


def read_json(path: Path) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        _assert_public(value)
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise CheckpointError("recovery checkpoint evidence is unreadable") from None


def read_private_json(path: Path) -> Any:
    """Read a local record with strict JSON syntax but without public-key filtering."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        return _unique_json_object(pairs)

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise CheckpointError("private recovery record is unreadable") from None
    _assert_finite(value)
    return value


def _assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise CheckpointError("recovery record contains a non-finite number")
    if isinstance(value, Mapping):
        for child in value.values():
            _assert_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_finite(child)


def _entropy(run_id: str, manifest_sha256: str) -> bytes:
    return hashlib.sha256(
        b"business-trust-v1\x00" + run_id.encode("utf-8") + b"\x00" + manifest_sha256.encode("ascii")
    ).digest()


def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise CheckpointError("resume secrets require Windows DPAPI current-user protection")

    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob_from(value: bytes) -> tuple[DataBlob, Any]:
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    input_blob, input_buffer = blob_from(data)
    entropy_blob, entropy_buffer = blob_from(entropy)
    output_blob = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN; default scope is the current user.
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR,
                             ctypes.POINTER(DataBlob), ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(DataBlob)]
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                             ctypes.POINTER(DataBlob), ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    try:
        ok = function(ctypes.byref(input_blob), None,
                      ctypes.byref(entropy_blob), None, None, flags, ctypes.byref(output_blob))
        if not ok:
            raise CheckpointError("recovery secret protection failed")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        # Keep the input buffers alive for the native call, and release only the
        # allocation returned by CryptProtectData/CryptUnprotectData.
        _ = input_buffer, entropy_buffer
        if output_blob.pbData:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(output_blob.pbData)


def write_secret_store(run_dir: Path, *, run_id: str, manifest_sha256: str,
                       secrets_payload: Mapping[str, Any]) -> str:
    """Persist credentials only as DPAPI ciphertext; return its ciphertext hash."""
    if not isinstance(secrets_payload, Mapping):
        raise CheckpointError("private recovery payload is invalid")
    envelope = {
        "schemaVersion": CHECKPOINT_SCHEMA_VERSION,
        "runId": run_id,
        "manifestSha256": manifest_sha256,
        "secrets": dict(secrets_payload),
    }
    try:
        plaintext = canonical_bytes(envelope)
    except (TypeError, ValueError):
        raise CheckpointError("private recovery payload is invalid") from None
    ciphertext = _dpapi(plaintext, _entropy(run_id, manifest_sha256), protect=True)
    target = secret_path(run_dir)
    atomic_write_bytes(target, ciphertext)
    return sha256_bytes(ciphertext)


def read_secret_store(run_dir: Path, *, run_id: str, manifest_sha256: str,
                      expected_ciphertext_sha256: str) -> dict[str, Any]:
    target = secret_path(run_dir)
    try:
        ciphertext = target.read_bytes()
    except OSError:
        raise CheckpointError("private recovery credentials are unavailable") from None
    if not ciphertext or sha256_bytes(ciphertext) != expected_ciphertext_sha256:
        raise CheckpointError("private recovery credentials failed integrity validation")
    plaintext = _dpapi(ciphertext, _entropy(run_id, manifest_sha256), protect=False)
    try:
        envelope = json.loads(plaintext.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_json_object(pairs),
                              parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise CheckpointError("private recovery credentials are invalid") from None
    if (not isinstance(envelope, dict)
        or set(envelope) != {"schemaVersion", "runId", "manifestSha256", "secrets"}
        or type(envelope.get("schemaVersion")) is not int
        or envelope.get("schemaVersion") != CHECKPOINT_SCHEMA_VERSION
        or envelope.get("runId") != run_id
        or envelope.get("manifestSha256") != manifest_sha256
        or not isinstance(envelope.get("secrets"), dict)):
        raise CheckpointError("private recovery credentials do not match this frozen run")
    return envelope["secrets"]


def read_ledger(run_dir: Path) -> dict[str, Any]:
    value = read_json(ledger_path(run_dir))
    if not isinstance(value, dict):
        raise CheckpointError("recovery ledger shape is invalid")
    _assert_public(value)
    return value


def write_ledger(run_dir: Path, ledger: Mapping[str, Any]) -> None:
    value = dict(ledger)
    _assert_public(value)
    write_json_atomic(ledger_path(run_dir), value)


def make_worker_ready_ledger(*, run_id: str, manifest_sha256: str,
                             secret_sha256: str, schedule_sha256: str,
                             dispatch_nonces_sha256: str,
                             dispatch_file: str, dispatch_sha256: str,
                             gate_hashes: Mapping[str, str],
                             binding_hashes: Mapping[str, str],
                             record_hashes: Mapping[str, str],
                             created_at_epoch: float | None = None) -> dict[str, Any]:
    now = time.time() if created_at_epoch is None else created_at_epoch
    return {
        "schemaVersion": CHECKPOINT_SCHEMA_VERSION,
        "protocol": RECOVERY_PROTOCOL["mode"],
        "runId": run_id,
        "manifestSha256": manifest_sha256,
        "privateBlobSha256": secret_sha256,
        "scheduleSha256": schedule_sha256,
        "dispatchNoncesSha256": dispatch_nonces_sha256,
        "dispatch": (None if not dispatch_file else {
            "file": dispatch_file, "sha256": dispatch_sha256,
        }),
        "gateHashes": dict(gate_hashes),
        "bindingHashes": dict(binding_hashes),
        "phase": "worker_ready",
        "revision": 1,
        "createdAtEpoch": now,
        "lastWallEpoch": now,
        "consumedJobs": {},
        "records": {job: {"revision": 0, "sha256": sha}
                    for job, sha in record_hashes.items()},
    }


def validate_worker_ready_ledger(ledger: Mapping[str, Any], *, run_id: str,
                                 manifest_sha256: str) -> None:
    expected_fields = {
        "schemaVersion", "protocol", "runId", "manifestSha256", "privateBlobSha256",
        "scheduleSha256", "dispatchNoncesSha256", "dispatch", "gateHashes",
        "bindingHashes", "phase", "revision", "createdAtEpoch", "lastWallEpoch",
        "consumedJobs", "records", "ledgerMac",
    }
    if (type(ledger.get("schemaVersion")) is not int
        or set(ledger) != expected_fields
        or ledger.get("schemaVersion") != CHECKPOINT_SCHEMA_VERSION
        or ledger.get("protocol") != RECOVERY_PROTOCOL["mode"]
        or ledger.get("runId") != run_id
        or ledger.get("manifestSha256") != manifest_sha256
        or ledger.get("phase") != "worker_ready"
        or type(ledger.get("revision")) is not int or ledger["revision"] < 1
        or not isinstance(ledger.get("consumedJobs"), dict)
        or not isinstance(ledger.get("records"), dict)
        or not isinstance(ledger.get("gateHashes"), dict)
        or not isinstance(ledger.get("bindingHashes"), dict)):
        raise CheckpointError("recovery ledger is not a valid worker-ready checkpoint")
    for key in ("manifestSha256", "privateBlobSha256", "scheduleSha256", "dispatchNoncesSha256"):
        if not _is_sha256(ledger.get(key)):
            raise CheckpointError("recovery ledger digest is invalid")
    if not _is_sha256(ledger.get("ledgerMac")):
        raise CheckpointError("recovery ledger authenticator is invalid")
    if not all(isinstance(name, str) and _is_sha256(value)
               for name, value in ledger["gateHashes"].items()):
        raise CheckpointError("recovery gate digest set is invalid")
    if not all(isinstance(name, str) and _is_sha256(value)
               for name, value in ledger["bindingHashes"].items()):
        raise CheckpointError("recovery binding digest set is invalid")
    dispatch = ledger.get("dispatch")
    if dispatch is not None and (
        not isinstance(dispatch, dict)
        or set(dispatch) != {"file", "sha256", "mode", "selectedJobIds"}
        or dispatch.get("file") not in {"app-dispatch-start.json", "runner-start.json"}
        or not _is_sha256(dispatch.get("sha256"))
        or not isinstance(dispatch.get("mode"), str)
        or not isinstance(dispatch.get("selectedJobIds"), list)
        or not all(isinstance(job, str) for job in dispatch["selectedJobIds"])
        or len(set(dispatch["selectedJobIds"])) != len(dispatch["selectedJobIds"])
    ):
        raise CheckpointError("recovery dispatch binding is invalid")
    for job, item in ledger["records"].items():
        if (not isinstance(job, str) or not isinstance(item, dict)
            or set(item) != {"revision", "sha256"}
            or type(item.get("revision")) is not int or item["revision"] < 0
            or not _is_sha256(item.get("sha256"))):
            raise CheckpointError("recovery record head is invalid")
    valid_statuses = {"running", "complete", "timeout", "invalid", "infrastructure_error"}
    for job, item in ledger["consumedJobs"].items():
        if (not isinstance(job, str) or not isinstance(item, dict)
            or set(item) != {"schedulePosition", "startedAtEpoch", "deadlineAtEpoch", "status"}
            or type(item.get("schedulePosition")) is not int or item["schedulePosition"] < 0
            or type(item.get("startedAtEpoch")) not in (int, float)
            or type(item.get("deadlineAtEpoch")) not in (int, float)
            or not math.isfinite(item["startedAtEpoch"])
            or not math.isfinite(item["deadlineAtEpoch"])
            or item["deadlineAtEpoch"] < item["startedAtEpoch"]
            or item.get("status") not in valid_statuses):
            raise CheckpointError("recovery consumed-position evidence is invalid")
    for field in ("createdAtEpoch", "lastWallEpoch"):
        if type(ledger.get(field)) not in (float, int) or not math.isfinite(ledger[field]):
            raise CheckpointError("recovery ledger clock evidence is invalid")


def next_ledger(ledger: Mapping[str, Any], **updates: Any) -> dict[str, Any]:
    value = dict(ledger)
    value.update(updates)
    value["revision"] = int(ledger["revision"]) + 1
    value["lastWallEpoch"] = time.time()
    _assert_public(value)
    return value


def check_wall_clock(ledger: Mapping[str, Any], now: float | None = None) -> float:
    observed = time.time() if now is None else now
    previous = ledger.get("lastWallEpoch")
    if (type(observed) not in (int, float) or not math.isfinite(observed)
        or type(previous) not in (int, float) or not math.isfinite(previous)
        or observed < previous):
        raise CheckpointError("system wall clock moved backwards since the last recovery checkpoint")
    return observed


def ledger_mac(ledger: Mapping[str, Any], key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise CheckpointError("recovery ledger authenticator is unavailable")
    unsigned = {name: value for name, value in ledger.items() if name != "ledgerMac"}
    return hmac.new(key, canonical_bytes(unsigned), hashlib.sha256).hexdigest()


def seal_ledger(ledger: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    unsigned = {name: value for name, value in ledger.items() if name != "ledgerMac"}
    return {**unsigned, "ledgerMac": ledger_mac(unsigned, key)}


def verify_ledger_mac(ledger: Mapping[str, Any], key: bytes) -> None:
    observed = ledger.get("ledgerMac")
    if not isinstance(observed, str) or not hmac.compare_digest(observed, ledger_mac(ledger, key)):
        raise CheckpointError("recovery ledger authentication failed")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def gate_hashes(run_dir: Path, filenames: tuple[str, ...]) -> dict[str, str]:
    output = {}
    for name in filenames:
        path = run_dir / name
        if not path.is_file():
            raise CheckpointError("required worker-ready gate evidence is missing")
        output[name] = sha256_file(path)
    return output


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value

