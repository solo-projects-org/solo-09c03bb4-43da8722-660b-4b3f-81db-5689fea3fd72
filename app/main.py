"""FastAPI application exposing the GTFS version publication API.

Guarantees provided to readers:

* Every request resolves versions and reads rows inside a single SQLite read
  transaction, so it observes one committed version — never a partially
  published one, and never a mix of two.
* A publication becomes visible all at once when its SQLite transaction
  commits; the previous version remains intact and queryable by version id.
* Uploaded bytes are staged and validated before the artifact is renamed into
  place; a failed registration rolls the artifact back, leaving no residue and
  no damage to existing versions.
* Downloads are served from the same open descriptor whose SHA-256 was checked
  against the registered digest, so a missing or tampered artifact is refused
  rather than served.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import diff
from .gtfs_parse import FeedRejected, validate_archive
from .store import (
    StorageError,
    Store,
    VersionInfo,
    iter_fd,
)

logger = logging.getLogger("gtfs.publisher")

DEFAULT_MAX_UPLOAD_BYTES = 256 * 1024 * 1024
UPLOAD_CHUNK = 1 * 1024 * 1024


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    get_store()  # opens/creates the DB and runs startup recovery
    yield


app = FastAPI(
    title="GTFS 时刻表发布服务",
    description=(
        "上传 GTFS ZIP 包（stops.txt、trips.txt、stop_times.txt）发布为新版本；"
        "旧版本在新版本发布后仍可按版号查询与下载。\n\n"
        "发布时校验：站点/班次 ID 唯一、停站必须引用存在的站点和班次、"
        "每站必须有到站时刻、同班次站序不得重复、时刻随站序不得倒退；"
        "超过 24 点的 GTFS 时刻（如 25:10:00）是合法的。"
        "校验失败会返回文件与行号，且不会改变任何已发布版本。\n\n"
        "另提供跨版线路对照：在两个版本间按停站序列一对一配对同线路班次，"
        "输出逐站到站时差与班次增减。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class VersionSummary(BaseModel):
    version_id: int = Field(description="版本号，发布后单调递增")
    created_at: str = Field(description="发布时间（UTC, ISO 8601）")
    upload_filename: str = Field(description="上传时的原始文件名")
    sha256: str = Field(description="ZIP 包 SHA-256 摘要")
    size_bytes: int = Field(description="ZIP 包字节数")
    stops_count: int
    trips_count: int
    stop_times_count: int


class VersionList(BaseModel):
    current_version: Optional[int] = Field(description="当前版本号，尚无版本时为 null")
    versions: List[VersionSummary]


class PublicationResult(BaseModel):
    version: VersionSummary
    previous_version: Optional[int] = Field(
        description="发布前查询的当前版本号（旧版仍可按版号访问）"
    )


class StopArrival(BaseModel):
    stop_sequence: int
    stop_id: str
    stop_name: str
    arrival_time: str = Field(description="GTFS 原始到站时刻，可超过 24:00:00")
    arrival_seconds: int = Field(description="自服务日起点的秒数")


class TripStops(BaseModel):
    version_id: int
    trip_id: str
    route_id: Optional[str]
    stops: List[StopArrival]


class StopTimeDiff(BaseModel):
    stop_id: str
    from_stop_sequence: int = Field(description="旧版中的站序")
    to_stop_sequence: int = Field(description="新版中的站序")
    from_arrival_time: str = Field(description="旧版到站时刻（GTFS 原始，可超过 24:00:00）")
    to_arrival_time: str = Field(description="新版到站时刻（GTFS 原始，可超过 24:00:00）")
    from_arrival_seconds: int = Field(description="旧版到站时刻（自服务日起点的秒数）")
    to_arrival_seconds: int = Field(description="新版到站时刻（自服务日起点的秒数）")
    diff_seconds: int = Field(description="到站时差（秒）：新版减旧版，正为晚点")


class TripPairDiff(BaseModel):
    from_trip_id: str = Field(description="旧版班次 ID")
    to_trip_id: str = Field(description="新版班次 ID（可能已重编）")
    first_stop_diff_seconds: int = Field(description="首站发车时差（秒）：新版减旧版")
    stops: List[StopTimeDiff] = Field(description="逐站到站对照（按站序对齐）")


class RouteDiff(BaseModel):
    route_id: str
    from_version: int = Field(description="实际解析出的旧版号")
    to_version: int = Field(description="实际解析出的新版号")
    max_offset_minutes: int = Field(description="首站偏移上限（分钟）")
    matched_pairs: List[TripPairDiff] = Field(
        description="配对成功的班次（停站序列相同且首站偏移不超限），按 trip_id 稳定排序"
    )
    removed_trips: List[str] = Field(description="旧版中退出（未能配对）的班次 ID")
    added_trips: List[str] = Field(description="新版中新增（未能配对）的班次 ID")


class ValidationFailure(BaseModel):
    error: str
    message: str
    errors: List[dict]


class ErrorResponse(BaseModel):
    error: str
    message: str


# ---------------------------------------------------------------------------
# Application state / wiring
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    return Path(os.environ.get("GTFS_DATA_DIR", "/data"))


_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        store = Store(_data_dir())
        store.init()
        _store = store
    return _store


def set_store(store: Store) -> None:
    """Used by tests to inject an isolated store."""
    global _store
    _store = store


def max_upload_bytes() -> int:
    raw = os.environ.get("GTFS_MAX_UPLOAD_BYTES")
    if raw is None:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@app.exception_handler(StorageError)
async def _storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    headers = {}
    if exc.status_code == 410:
        headers["Cache-Control"] = "no-store"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(info: VersionInfo) -> VersionSummary:
    return VersionSummary(
        version_id=info.version_id,
        created_at=info.created_at,
        upload_filename=info.upload_filename,
        sha256=info.sha256,
        size_bytes=info.size_bytes,
        stops_count=info.stops_count,
        trips_count=info.trips_count,
        stop_times_count=info.stop_times_count,
    )


def _safe_upload_name(raw: Optional[str]) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        name = "feed.zip"
    # Keep the persisted name printable and bounded; it is never used as a path.
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    return name[:200]


def _invalid_upload(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_failed",
            "message": message,
            "errors": [
                {
                    "file": "",
                    "line": 0,
                    "code": "invalid_archive",
                    "message": message,
                }
            ],
        },
    )


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/versions",
    response_model=PublicationResult,
    status_code=201,
    summary="发布新的 GTFS 版本",
    responses={
        422: {
            "model": ValidationFailure,
            "description": "GTFS 校验失败，errors 中包含文件与行号",
        },
        413: {"model": ErrorResponse, "description": "上传包超过大小限制"},
    },
)
async def publish_version(
    file: UploadFile = File(..., description="包含 stops.txt、trips.txt、stop_times.txt 的 ZIP"),
) -> JSONResponse:
    store = get_store()
    upload_name = _safe_upload_name(file.filename)
    limit = max_upload_bytes()

    # Stage the upload under a unique name; nothing is registered yet.
    fd, staging_name = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=store.staging_dir)
    staging_path = Path(staging_name)
    sha256 = hashlib.sha256()
    size = 0
    try:
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await file.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        return JSONResponse(
                            status_code=413,
                            content={
                                "error": "upload_too_large",
                                "message": f"upload exceeds the {limit} byte limit",
                            },
                        )
                    sha256.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        finally:
            await file.close()

        if size == 0:
            return _invalid_upload("uploaded file is empty")

        # Validation and registration are blocking (file IO, SQLite write lock),
        # so they run off the event loop; queries stay responsive meanwhile.
        try:
            info, previous = await run_in_threadpool(
                _validate_and_publish,
                store,
                staging_path,
                sha256.hexdigest(),
                size,
                upload_name,
            )
        except FeedRejected as rejected:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "validation_failed",
                    "message": f"{len(rejected.errors)} validation error(s); "
                    "no version was published",
                    "errors": rejected.errors,
                },
            )
        except zipfile.BadZipFile:
            return _invalid_upload("uploaded file is not a valid ZIP archive")
        except Exception:
            logger.exception("publication failed")
            # Registration failed; publish() already removed the artifact.
            return JSONResponse(
                status_code=500,
                content={
                    "error": "publication_failed",
                    "message": "the feed could not be registered; the previous "
                    "version is unchanged",
                },
            )
        staging_path = None  # ownership moved into the store
        return JSONResponse(
            status_code=201,
            content={
                "version": _summary(info).model_dump(),
                "previous_version": previous,
            },
        )
    finally:
        if staging_path is not None:
            try:
                staging_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _validate_and_publish(
    store: Store,
    staging_path: Path,
    sha256: str,
    size: int,
    upload_name: str,
):
    """Blocking part of the publish path, executed in a worker thread."""
    with zipfile.ZipFile(staging_path, "r") as zf:
        feed = validate_archive(zf, upload_name)
    return store.publish(
        staging_path=staging_path,
        sha256=sha256,
        size_bytes=size,
        upload_filename=upload_name,
        feed=feed,
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/versions",
    response_model=VersionList,
    summary="列出版本（新版本在前）",
)
def list_versions() -> VersionList:
    store = get_store()
    versions, current = store.list_versions()
    return VersionList(
        current_version=current, versions=[_summary(v) for v in versions]
    )


@app.get(
    "/api/v1/versions/{version}",
    response_model=VersionSummary,
    summary="查看某个版本的信息",
    responses={404: {"model": ErrorResponse}},
)
def get_version(version: str) -> VersionSummary:
    info, _current = get_store().get_version(version)
    return _summary(info)


@app.get(
    "/api/v1/versions/{version}/trips/{trip_id}/stops",
    response_model=TripStops,
    summary="按版本号或 current 查询某班次的完整停站",
    responses={404: {"model": ErrorResponse}},
)
def trip_stops_by_version(version: str, trip_id: str) -> TripStops:
    info, route_id, stops = get_store().trip_stops(version, trip_id)
    return TripStops(
        version_id=info.version_id, trip_id=trip_id, route_id=route_id, stops=stops
    )


@app.get(
    "/api/v1/trips/{trip_id}/stops",
    response_model=TripStops,
    summary="查询班次停站（version 省略时使用当前版本）",
    responses={404: {"model": ErrorResponse}},
)
def trip_stops_current(
    trip_id: str,
    version: str = Query(
        "current",
        description="版本号或 current（默认）",
        examples=["current", "1"],
    ),
) -> TripStops:
    info, route_id, stops = get_store().trip_stops(version, trip_id)
    return TripStops(
        version_id=info.version_id, trip_id=trip_id, route_id=route_id, stops=stops
    )


# ---------------------------------------------------------------------------
# Cross-version route comparison
# ---------------------------------------------------------------------------


def _to_schedule(trip: dict) -> diff.TripSchedule:
    return diff.TripSchedule(
        trip_id=trip["trip_id"],
        stops=[diff.TripStop(**stop) for stop in trip["stops"]],
    )


@app.get(
    "/api/v1/routes/{route_id}/diff",
    response_model=RouteDiff,
    summary="跨版本对照某条线路的班次增减与到站变化",
    description=(
        "在两个已发布版本（可用 current）之间对照一条线路：仅在完整停站 "
        "stop_id 序列相同的班次间一对一配对（班次 ID 可已重编），"
        "首站偏移不超过 max_offset_minutes 分钟；先使配对数最多，"
        "再使首站时间差总和最小。返回配对班次的逐站到站时差（秒），"
        "并分别列出旧版退出与新版新增的班次。超过 24 点的时刻按服务日秒比较。"
        "版号解析与两版数据读取处于同一快照，发布交错不会混入新的 current。"
    ),
    responses={
        400: {"model": ErrorResponse, "description": "版号格式非法"},
        404: {"model": ErrorResponse, "description": "版本或线路不存在"},
        422: {"model": ErrorResponse, "description": "参数非法（如偏移分钟为负）"},
    },
)
def route_diff(
    route_id: str,
    from_version: str = Query(
        ...,
        alias="from",
        description="旧版号或 current",
        examples=["1", "current"],
    ),
    to_version: str = Query(
        "current",
        alias="to",
        description="新版号或 current（默认 current）",
        examples=["2", "current"],
    ),
    max_offset_minutes: int = Query(
        0, ge=0, description="最大首站偏移分钟数（非负，默认 0 即首站时刻须相同）"
    ),
) -> RouteDiff:
    store = get_store()
    from_id, to_id, from_rows, to_rows = store.route_schedules_for_diff(
        from_version, to_version, route_id
    )
    result = diff.compute_route_diff(
        [_to_schedule(trip) for trip in from_rows],
        [_to_schedule(trip) for trip in to_rows],
        max_offset_minutes * 60,
    )
    return RouteDiff(
        route_id=route_id,
        from_version=from_id,
        to_version=to_id,
        max_offset_minutes=max_offset_minutes,
        matched_pairs=[
            TripPairDiff(
                from_trip_id=pair.from_trip.trip_id,
                to_trip_id=pair.to_trip.trip_id,
                first_stop_diff_seconds=(
                    pair.to_trip.first_arrival_seconds
                    - pair.from_trip.first_arrival_seconds
                ),
                stops=[
                    StopTimeDiff(
                        stop_id=old_stop.stop_id,
                        from_stop_sequence=old_stop.stop_sequence,
                        to_stop_sequence=new_stop.stop_sequence,
                        from_arrival_time=old_stop.arrival_time,
                        to_arrival_time=new_stop.arrival_time,
                        from_arrival_seconds=old_stop.arrival_seconds,
                        to_arrival_seconds=new_stop.arrival_seconds,
                        diff_seconds=(
                            new_stop.arrival_seconds - old_stop.arrival_seconds
                        ),
                    )
                    # Pairing guarantees identical stop_id sequences.
                    for old_stop, new_stop in zip(
                        pair.from_trip.stops, pair.to_trip.stops
                    )
                ],
            )
            for pair in result.pairings
        ],
        removed_trips=result.removed_trip_ids,
        added_trips=result.added_trip_ids,
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download_headers(info: VersionInfo) -> dict:
    return {
        "Content-Disposition": f'attachment; filename="gtfs-version-{info.version_id}.zip"',
        "Content-Length": str(info.size_bytes),
        "Content-Type": "application/zip",
        "ETag": f'"{info.sha256}"',
        "X-GTFS-Version": str(info.version_id),
        "X-Content-SHA256": info.sha256,
        "Cache-Control": "no-store",
    }


@app.get(
    "/api/v1/versions/{version}/download",
    summary="下载该版本原始 ZIP（校验摘要后返回）",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "摘要不符"},
        410: {"model": ErrorResponse, "description": "文件缺失"},
    },
)
def download_version(version: str):
    store = get_store()
    info, _current = store.get_version(version)
    # Verify the whole artifact before any bytes go on the wire; the returned
    # descriptor is the one that gets streamed, so the bytes cannot change
    # between verification and transmission.  StorageError subclasses
    # (missing -> 410, digest mismatch -> 409) are handled by the global handler.
    fd = store.open_verified_artifact(info)

    filename = f"gtfs-version-{info.version_id}.zip"
    headers = _download_headers(info)
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return StreamingResponse(
        iter_fd(fd, info.size_bytes),
        media_type="application/zip",
        headers=headers,
    )


@app.get("/healthz", summary="健康检查")
def healthz() -> dict:
    store = get_store()
    try:
        versions, current = store.list_versions()
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(exc)},
        )
    return {"status": "ok", "versions": len(versions), "current_version": current}
