"""Private-only YouTube uploads: OAuth credentials, quota ledger, resumable upload.

Every upload is ``privacyStatus: private``. Publishing is a human decision made in
YouTube Studio; nothing here can make a video public or schedule it.

Google client libraries are imported lazily so tests run with injected fakes.
Needed at runtime: google-api-python-client, google-auth, google-auth-oauthlib
(only for ``authorize``), azure-keyvault-secrets + azure-identity (only for
``KeyVaultSecretProvider``).
"""
from __future__ import annotations

import http.client
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..models import PipelineError

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"

PRIVACY = "private"
# Verified on https://developers.google.com/youtube/v3/docs/videos (status.containsSyntheticMedia: boolean).
SYNTHETIC_MEDIA_FIELD = "containsSyntheticMedia"
DEFAULT_CATEGORY = "24"  # Entertainment
LANGUAGE = "ko"
CAPTION_NAME = "한국어"

# Source: https://developers.google.com/youtube/v3/determine_quota_cost (checked 2026-10-09).
# videos.insert moved to its own bucket (100 calls/day, 1 per call); it was 1600 general units before.
# If Google changes it back, edit this table only: e.g. "videos.insert": ("units", 1600).
QUOTA_COSTS: dict[str, tuple[str, int]] = {
    "videos.insert": ("uploads", 1),
    "thumbnails.set": ("units", 50),
    "captions.insert": ("units", 400),
}
DEFAULT_DAILY_UNITS = 10_000
DEFAULT_DAILY_UPLOADS = 100

TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
CHUNK_BYTES = 16 * 1024 * 1024
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
WATCH_URL = "https://www.youtube.com/watch?v={}"


# -- secrets and credentials ---------------------------------------------------
class SecretProvider(Protocol):
    def get(self, name: str) -> str: ...


@dataclass(frozen=True)
class SecretNames:
    client_id: str = "youtube-client-id"
    client_secret: str = "youtube-client-secret"
    refresh_token: str = "youtube-refresh-token"


class EnvSecretProvider:
    """Local provider: secret ``youtube-client-id`` is read from ``YOUTUBE_CLIENT_ID``."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        self.environ = os.environ if environ is None else environ

    def get(self, name: str) -> str:
        variable = name.upper().replace("-", "_")
        value = self.environ.get(variable, "").strip()
        if not value:
            raise PipelineError(f"Environment variable {variable} (secret {name!r}) is not set.")
        return value


class KeyVaultSecretProvider:
    def __init__(self, vault_url: str, client: Any = None):
        if client is None:
            if not vault_url:
                raise PipelineError("Key Vault URL is not configured.")
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
            except ImportError as error:
                raise PipelineError("Install azure-keyvault-secrets and azure-identity to read Key Vault.") from error
            client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
        self.client = client

    def get(self, name: str) -> str:
        try:
            value = self.client.get_secret(name).value
        except Exception as error:
            raise PipelineError(f"Cannot read Key Vault secret {name!r}: {error}") from error
        if not value:
            raise PipelineError(f"Key Vault secret {name!r} is empty.")
        return value


def youtube_credentials(provider: SecretProvider, names: SecretNames = SecretNames(),
                        credentials_cls: Any = None) -> Any:
    if credentials_cls is None:
        try:
            from google.oauth2.credentials import Credentials as credentials_cls
        except ImportError as error:
            raise PipelineError("Install google-auth to build YouTube credentials.") from error
    return credentials_cls(
        token=None, refresh_token=provider.get(names.refresh_token), token_uri=TOKEN_URI,
        client_id=provider.get(names.client_id), client_secret=provider.get(names.client_secret),
        scopes=list(SCOPES),
    )


def build_services(credentials: Any) -> tuple[Any, Any]:
    """Returns (YouTube Data API v3, YouTube Analytics API v2) clients."""
    try:
        from googleapiclient.discovery import build
    except ImportError as error:
        raise PipelineError("Install google-api-python-client.") from error
    return (build("youtube", "v3", credentials=credentials, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False))


def authorize(client_secrets_path: Path | str, port: int = 0) -> str:
    """One-time, interactive, on the operator's machine. Opens a browser consent page for
    the channel owner and returns a refresh token to store as secret ``youtube-refresh-token``.

    Usage: ``python -c "from story_pipeline.studio.youtube import authorize; print(authorize('client_secret.json'))"``
    The client secrets file is a Google Cloud "Desktop app" OAuth client. Never commit it or the token.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise PipelineError("Install google-auth-oauthlib to run the one-time authorization.") from error
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), scopes=list(SCOPES))
    credentials = flow.run_local_server(port=port, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise PipelineError("Google returned no refresh token; revoke the app grant and authorize again.")
    return credentials.refresh_token


# -- quota ---------------------------------------------------------------------
def _nth_sunday(year: int, month: int, nth: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def pacific_day(moment: datetime) -> str:
    """Quota day (resets at midnight Pacific). US DST rule since 2007; avoids a tzdata dependency."""
    if moment.tzinfo is None:
        raise PipelineError("pacific_day needs a timezone-aware datetime.")
    utc = moment.astimezone(timezone.utc)
    start = datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=10)
    end = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=9)
    offset = -7 if start <= utc < end else -8
    return (utc + timedelta(hours=offset)).date().isoformat()


def plan_cost(operations: Iterable[str]) -> dict[str, int]:
    cost = {"units": 0, "uploads": 0}
    for operation in operations:
        try:
            bucket, amount = QUOTA_COSTS[operation]
        except KeyError:
            raise PipelineError(f"No quota cost known for {operation!r}.") from None
        cost[bucket] += amount
    return cost


class QuotaLedger:
    """Daily YouTube Data API quota usage, persisted as JSON. Single-writer."""

    def __init__(self, path: Path, daily_units: int = DEFAULT_DAILY_UNITS,
                 daily_uploads: int = DEFAULT_DAILY_UPLOADS,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if daily_units <= 0 or daily_uploads <= 0:
            raise PipelineError("Daily quota limits must be positive.")
        self.path = path
        self.limits = {"units": daily_units, "uploads": daily_uploads}
        self.clock = clock
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1:
                raise PipelineError(f"Unsupported quota ledger schema in {path}.")
            self.days: dict[str, dict[str, Any]] = data["days"]
        else:
            self.days = {}

    def today(self) -> str:
        return pacific_day(self.clock())

    def used(self) -> dict[str, int]:
        day = self.days.get(self.today(), {})
        return {"units": day.get("units", 0), "uploads": day.get("uploads", 0)}

    def remaining(self) -> dict[str, int]:
        used = self.used()
        return {bucket: self.limits[bucket] - used[bucket] for bucket in self.limits}

    def shortfall(self, cost: Mapping[str, int]) -> str | None:
        remaining = self.remaining()
        short = [f"{b}: need {cost.get(b, 0)}, remaining {remaining[b]}"
                 for b in self.limits if cost.get(b, 0) > remaining[b]]
        return "; ".join(short) or None

    def charge(self, operation: str) -> None:
        """Charged at attempt time: Google bills failed requests too."""
        cost = plan_cost([operation])
        reason = self.shortfall(cost)
        if reason:
            raise PipelineError(f"{operation} would exceed today's quota ({reason}).")
        day = self.days.setdefault(self.today(), {"units": 0, "uploads": 0, "operations": []})
        for bucket, amount in cost.items():
            day[bucket] += amount
        day["operations"].append({"operation": operation, "at": self.clock().isoformat()})
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "limits": self.limits, "days": self.days},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


# -- metadata ------------------------------------------------------------------
def tags_length(tags: Sequence[str]) -> int:
    """YouTube counts commas between tags and quotes around tags containing spaces."""
    return sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(0, len(tags) - 1)


def validate_metadata(metadata: Mapping[str, Any], label: str = "video") -> None:
    forbidden = {"privacyStatus", "publishAt", "privacy", "status"} & set(metadata)
    if forbidden:
        raise PipelineError(f"{label}: metadata may not set {sorted(forbidden)}; uploads are always private.")
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PipelineError(f"{label}: title is required.")
    if len(title) > TITLE_MAX_CHARS:
        raise PipelineError(f"{label}: title has {len(title)} characters; the limit is {TITLE_MAX_CHARS}.")
    description = metadata.get("description", "")
    if not isinstance(description, str):
        raise PipelineError(f"{label}: description must be a string.")
    if len(description.encode("utf-8")) > DESCRIPTION_MAX_BYTES:
        raise PipelineError(f"{label}: description exceeds {DESCRIPTION_MAX_BYTES} UTF-8 bytes.")
    tags = metadata.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
        raise PipelineError(f"{label}: tags must be a list of nonempty strings.")
    if tags_length(tags) > TAGS_MAX_CHARS:
        raise PipelineError(f"{label}: tags total {tags_length(tags)} characters; the limit is {TAGS_MAX_CHARS}.")
    for field, value in (("title", title), ("description", description), *(("tag", t) for t in tags)):
        if "<" in value or ">" in value:
            raise PipelineError(f"{label}: {field} may not contain '<' or '>'.")


def video_body(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "snippet": {
            "title": metadata["title"], "description": metadata.get("description", ""),
            "tags": list(metadata.get("tags", [])),
            "categoryId": str(metadata.get("category_id", DEFAULT_CATEGORY)),
            "defaultLanguage": LANGUAGE, "defaultAudioLanguage": LANGUAGE,
        },
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False, SYNTHETIC_MEDIA_FIELD: True},
    }


def short_metadata(short: Mapping[str, Any], main_video_id: str, main: Mapping[str, Any]) -> dict[str, Any]:
    title = short["title"].strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    description = short.get("description", "").strip()
    link = f"전체 에피소드: {WATCH_URL.format(main_video_id)}"
    parts = [p for p in (description, link) if p]
    if "#shorts" not in description.lower():
        parts.append("#Shorts")
    return {"title": title, "description": "\n\n".join(parts), "tags": list(short.get("tags", main.get("tags", []))),
            "category_id": main.get("category_id", DEFAULT_CATEGORY)}


# -- upload ----------------------------------------------------------------------
def _status(error: BaseException) -> int | None:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None) or getattr(error, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retryable(error: BaseException) -> bool:
    status = _status(error)
    if status is not None:
        return status in RETRYABLE_STATUS
    if isinstance(error, (ConnectionError, TimeoutError, http.client.HTTPException)):
        return True
    return any(cls.__name__ == "HttpLib2Error" for cls in type(error).__mro__)


class Retry:
    def __init__(self, max_retries: int = 5, base_seconds: float = 1.0, max_seconds: float = 64.0,
                 sleep: Callable[[float], None] = time.sleep, rng: Callable[[], float] = random.random):
        self.max_retries, self.base, self.cap, self.sleep, self.rng = max_retries, base_seconds, max_seconds, sleep, rng

    def run(self, label: str, action: Callable[[], Any]) -> Any:
        attempt = 0
        while True:
            try:
                return action()
            except PipelineError:
                raise
            except Exception as error:
                if not _retryable(error):
                    raise PipelineError(f"{label} failed: {error}") from error
                if attempt >= self.max_retries:
                    raise PipelineError(f"{label} failed after {attempt} retries: {error}") from error
                attempt += 1
                self.sleep(min(self.cap, self.base * 2 ** attempt) * (0.5 + self.rng() / 2))


def _default_media(path: str, mimetype: str, resumable: bool) -> Any:
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise PipelineError("Install google-api-python-client.") from error
    return MediaFileUpload(path, mimetype=mimetype, chunksize=CHUNK_BYTES if resumable else -1, resumable=resumable)


MediaFactory = Callable[[str, str, bool], Any]


def _insert_video(service: Any, path: Path, metadata: Mapping[str, Any], quota: QuotaLedger,
                  media: MediaFactory, retry: Retry) -> str:
    request = service.videos().insert(part="snippet,status", body=video_body(metadata),
                                      media_body=media(str(path), "video/*", True))
    quota.charge("videos.insert")
    response = None
    while response is None:
        _, response = retry.run(f"Uploading {path.name}", request.next_chunk)
    video_id = response.get("id") if isinstance(response, dict) else None
    if not video_id:
        raise PipelineError(f"Upload of {path.name} finished without a video id: {response!r}")
    privacy = response.get("status", {}).get("privacyStatus", PRIVACY)
    if privacy != PRIVACY:
        raise PipelineError(f"Video {video_id} came back as {privacy!r}, not private. Check it in YouTube Studio now.")
    return video_id


def _captions(paths: Path | Sequence[Path | Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if paths is None:
        return []
    items = [paths] if isinstance(paths, (str, Path)) else list(paths)
    tracks = []
    for index, item in enumerate(items):
        entry = {"path": item} if isinstance(item, (str, Path)) else dict(item)
        default_name = CAPTION_NAME if index == 0 else f"{CAPTION_NAME} {index + 1}"
        tracks.append({"path": Path(entry["path"]), "language": entry.get("language", LANGUAGE),
                       "name": entry.get("name", default_name)})
    keys = [(t["language"], t["name"]) for t in tracks]
    if len(set(keys)) != len(keys):
        raise PipelineError("Caption tracks need distinct (language, name) pairs.")
    return tracks


def _require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise PipelineError(f"{label} not found or empty: {path}")


def upload_episode(service: Any, video_path: Path, metadata: Mapping[str, Any], thumbnail_path: Path | None,
                   captions_paths: Path | Sequence[Path | Mapping[str, Any]] | None,
                   shorts: Sequence[Mapping[str, Any]], quota: QuotaLedger, *,
                   media_factory: MediaFactory | None = None, retry: Retry | None = None,
                   clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, Any]:
    """Uploads the episode and its shorts as PRIVATE videos.

    Returns ``{"status": "deferred", ...}`` without calling the API when today's quota
    cannot cover the whole plan; raises PipelineError on invalid input or API failure.
    ``shorts`` items: ``{"path", "title", "description"?, "tags"?}``.
    """
    media = media_factory or _default_media
    retry = retry or Retry()
    video_path = Path(video_path)
    _require_file(video_path, "Video")
    validate_metadata(metadata)
    if thumbnail_path is not None:
        thumbnail_path = Path(thumbnail_path)
        _require_file(thumbnail_path, "Thumbnail")
        if thumbnail_path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise PipelineError("Thumbnail must be PNG or JPEG.")
        if thumbnail_path.stat().st_size > THUMBNAIL_MAX_BYTES:
            raise PipelineError("Thumbnail exceeds YouTube's 2 MB limit.")
    tracks = _captions(captions_paths)
    for track in tracks:
        _require_file(track["path"], "Caption file")
    for index, short in enumerate(shorts):
        if "path" not in short or not isinstance(short.get("title"), str):
            raise PipelineError(f"Short {index + 1} needs 'path' and 'title'.")
        _require_file(Path(short["path"]), f"Short {index + 1} video")
        validate_metadata(short_metadata(short, "X" * 11, metadata), f"short {index + 1}")

    operations = ["videos.insert"] * (1 + len(shorts)) + ["captions.insert"] * len(tracks)
    if thumbnail_path is not None:
        operations.append("thumbnails.set")
    cost = plan_cost(operations)
    reason = quota.shortfall(cost)
    if reason:
        return {"status": "deferred", "reason": f"Daily YouTube quota insufficient ({reason}); retry after "
                f"midnight Pacific.", "quota_needed": cost, "quota_day": quota.today(), "privacy": PRIVACY}

    video_id = _insert_video(service, video_path, metadata, quota, media, retry)
    try:
        if thumbnail_path is not None:
            mimetype = "image/png" if thumbnail_path.suffix.lower() == ".png" else "image/jpeg"
            request = service.thumbnails().set(videoId=video_id, media_body=media(str(thumbnail_path), mimetype, False))
            quota.charge("thumbnails.set")
            retry.run("Setting thumbnail", request.execute)
        caption_ids = []
        for track in tracks:
            request = service.captions().insert(
                part="snippet",
                body={"snippet": {"videoId": video_id, "language": track["language"], "name": track["name"],
                                  "isDraft": False}},
                media_body=media(str(track["path"]), "application/octet-stream", False))
            quota.charge("captions.insert")
            caption_ids.append(retry.run(f"Uploading captions {track['path'].name}", request.execute).get("id"))
        short_ids = [_insert_video(service, Path(short["path"]), short_metadata(short, video_id, metadata),
                                   quota, media, retry) for short in shorts]
    except PipelineError as error:
        raise PipelineError(f"{error} (main video {video_id} is already uploaded as private)") from error
    return {"status": "uploaded", "video_id": video_id, "shorts": short_ids, "captions": caption_ids,
            "thumbnail_set": thumbnail_path is not None, "uploaded_at": clock().isoformat(),
            "privacy": PRIVACY, "quota_used": cost, "quota_day": quota.today()}


# -- audit -----------------------------------------------------------------------
@dataclass(frozen=True)
class YouTubeSettings:
    project_purpose: str = ("Uploads the operator's own AI-assisted Korean audio-drama episodes to the operator's "
                            "own channel as private videos for human review, and reads that channel's analytics.")
    episodes_per_day: float = 1.0
    shorts_per_episode: int = 3
    caption_tracks: int = 1
    thumbnail: bool = True
    daily_units: int = DEFAULT_DAILY_UNITS
    daily_uploads: int = DEFAULT_DAILY_UPLOADS


def audit_packet(config: YouTubeSettings) -> dict[str, Any]:
    """Facts the YouTube API Services audit/quota form asks for."""
    per_episode = ["videos.insert"] * (1 + config.shorts_per_episode) + ["captions.insert"] * config.caption_tracks
    if config.thumbnail:
        per_episode.append("thumbnails.set")
    episode_cost = plan_cost(per_episode)
    daily = {k: round(v * config.episodes_per_day, 2) for k, v in episode_cost.items()}
    return {
        "project_purpose": config.project_purpose,
        "scopes": list(SCOPES),
        "api_methods": sorted(set(per_episode)) + ["youtubeAnalytics.reports.query"],
        "privacy": "All uploads are private; publishing is done manually in YouTube Studio.",
        "synthetic_media_disclosure": f"status.{SYNTHETIC_MEDIA_FIELD}=true on every upload",
        "users": "Single channel owner (internal tool); no third-party user data.",
        "quota_per_episode": episode_cost,
        "estimated_daily_usage": daily,
        "daily_limits": {"units": config.daily_units, "uploads": config.daily_uploads},
        "within_default_quota": daily["units"] <= config.daily_units and daily["uploads"] <= config.daily_uploads,
        "quota_cost_source": "https://developers.google.com/youtube/v3/determine_quota_cost",
        "note": "youtubeAnalytics.reports.query is a separate API and is not counted in the Data API units above.",
    }
