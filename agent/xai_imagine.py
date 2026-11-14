"""A small client for xAI's Grok Imagine API: text to image (now or deferred) and image to video (started, then polled).

Never run against the live xAI API: no key exists and every image and video is paid, so nothing here has been confirmed by the real
service. Tests use tests/xai_standin.py, a server written from the pages saved in docs/vendor-contract/xai-*.txt (retrieved
2026-11-15). docs/xai-grok.md lists what had to be chosen because no saved page says it. This is a library, not wired into the
support agent: the agent answers customers and makes no pictures.

  Image (xai-image-generation.txt, xai-api-reference-images.txt): "POST https://api.x.ai/v1/images/generations" with model
    grok-imagine-image-2.0, prompt, n ("1 to 10", optional, default 1), aspect_ratio (default "auto"), resolution ("1k" default,
    "2k"; the reference also lists "1.5k"), quality ("low", "medium" or "auto", "only supported for grok-imagine-image-2.0") and
    response_format ("url" default, or "b64_json"). The answer is {"data": [{"url" or "b64_json", "mime_type"}], "usage":
    {"cost_in_usd_ticks"}}; "URLs are temporary, so download or process promptly". One USD cent is 100,000,000 ticks.
  Deferred images (same pages): "deferred": true returns a request_id, "Deferred requests support response_format: url only",
    "GET /v1/images/{request_id}" answers 202 pending, 200 done (data and usage) or 200 failed (error with code and message): "a
    failed generation is a body, not an HTTP error, so pollers do not retry-storm". Results are kept 24 hours.
  Video (xai-video-generation.txt, xai-image-to-video.txt, xai-api-reference-videos.txt): "POST /v1/videos/generations" with model
    grok-imagine-video-1.5, prompt (optional with an image), image {"url": public URL or base64 data URI}, duration (integer 1 to 15,
    default 8), aspect_ratio, resolution (480p, 720p, 1080p) answers {"request_id"}; "GET /v1/videos/{request_id}" answers a status
    of pending, done or failed; done carries video.url, video.duration and video.respect_moderation ("Otherwise the field will be
    false and the video url field will be empty"); failed carries error.code and error.message; pending may carry progress 0 to 99.
    "Keep polling every few seconds"; the samples sleep 5 s and the SDK default timeout is 10 minutes.

Statuses, backoff and what is never shown of a failure: see xai_http.py. A paid POST is never repeated after a dropped connection,
because the server may already have billed it; a poll (a GET) is.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import XAI_DEFAULT_URL, XAI_IMAGE_MODEL, XAI_VIDEO_MODEL
from .xai_http import (MAX_RETRIES, XaiBadResponseError, XaiConfigError, XaiError, XaiHTTP, checked_model, is_number, json_object)

IMAGE_ASPECT_RATIOS = ("1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1", "21:9",
                       "5:2", "auto")
IMAGE_RESOLUTIONS = ("1k", "1.5k", "2k")
IMAGE_QUALITIES = ("low", "medium", "auto")
QUALITY_MODEL = "grok-imagine-image-2.0"
RESPONSE_FORMATS = ("url", "b64_json")
MAX_IMAGES = 10
VIDEO_ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "5:2")
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
MIN_SECONDS, MAX_SECONDS = 1, 15
REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")  # the samples show a UUID; it goes into a URL path, so nothing else is allowed
IMAGE_SOURCE_OK = re.compile(r"^(https?://\S+|data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+)\Z")
MIME_OK = re.compile(r"^image/[a-z0-9.+-]{1,20}\Z")
POLL_SECONDS = 5.0 # "Keep polling every few seconds": the samples sleep 5
POLL_TIMEOUT_SECONDS = 600.0  # the SDK's default timeout for a video is 10 minutes
ERROR_TEXT_CHARS = 200
JOB_ERROR_CODES = ("invalid_argument", "permission_denied", "failed_precondition", "service_unavailable", "internal_error")
TICKS_PER_USD = 10_000_000_000  # "One USD cent equals 100,000,000 ticks, so one US dollar equals 10,000,000,000 ticks"


class XaiJobFailedError(XaiError):
    """A deferred image or a video ended with status failed. `code` is the documented error code."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class XaiModerationError(XaiError):
    """The video came back with respect_moderation false, so it has no URL."""


class XaiTimeoutError(XaiError):
    """The job was still pending when the wait ran out. `request_id` is kept so it can be polled again."""

    def __init__(self, message: str, *, request_id: str):
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True)
class GeneratedImage:
    url: str | None
    b64_json: str | None
    mime_type: str | None

    def image_bytes(self) -> bytes:
        if not self.b64_json:
            raise XaiConfigError("this image came back as a URL, not as base64")
        return base64.b64decode(self.b64_json, validate=True)


@dataclass(frozen=True)
class ImageBatch:
    images: tuple[GeneratedImage, ...]
    cost_usd_ticks: int | None = None
    request_id: str = ""

    @property
    def cost_usd(self) -> float | None:
        return None if self.cost_usd_ticks is None else self.cost_usd_ticks / TICKS_PER_USD


@dataclass(frozen=True)
class VideoResult:
    url: str
    duration: int | None
    cost_usd_ticks: int | None = None
    request_id: str = ""


@dataclass(frozen=True)
class Poll:
    done: bool
    progress: int | None = None
    result: Any = None


def _check_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not REQUEST_ID_OK.match(request_id):
        raise XaiConfigError("request_id is malformed")
    return request_id


def _prompt(prompt: object, *, required: bool = True) -> str | None:
    if prompt is None and not required:
        return None
    if not isinstance(prompt, str) or not prompt.strip():
        raise XaiConfigError("prompt must be a non-empty string")
    return prompt


def _choice(value: str | None, allowed: tuple[str, ...], name: str) -> None:
    if value is not None and value not in allowed:
        raise XaiConfigError("%s must be one of: %s" % (name, ", ".join(allowed)))


def _ticks(data: dict) -> int | None:
    usage = data.get("usage")
    ticks = usage.get("cost_in_usd_ticks") if isinstance(usage, dict) else None
    return ticks if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 0 else None


def _job_error(data: dict, what: str) -> XaiJobFailedError:
    err = data.get("error")
    code = err.get("code") if isinstance(err, dict) else None
    message = err.get("message") if isinstance(err, dict) else None
    code = code if code in JOB_ERROR_CODES else "unknown"
    text = " ".join(message.split())[:ERROR_TEXT_CHARS] if isinstance(message, str) else "no message"
    return XaiJobFailedError("%s failed: %s: %s" % (what, code, text), code=code)


class XaiImagine:
    def __init__(self, key: str, *, base_url: str = XAI_DEFAULT_URL, image_model: str = XAI_IMAGE_MODEL, video_model: str = XAI_VIDEO_MODEL,
                 timeout: float = 120.0, max_retries: int = MAX_RETRIES, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None, poll_seconds: float = POLL_SECONDS, poll_timeout: float = POLL_TIMEOUT_SECONDS):
        self._image_model = checked_model(image_model, "the image model name")
        self._video_model = checked_model(video_model, "the video model name")
        self._http = XaiHTTP(key, base_url, timeout=timeout, max_retries=max_retries, sleep=sleep)
        self._sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))
        self._clock = clock if clock is not None else time.monotonic
        self._poll_seconds, self._poll_timeout = poll_seconds, poll_timeout

    # ---- images ----------------------------------------------------------------------------------------------------------

    def _image_body(self, prompt: str, n: int | None, aspect_ratio: str | None, resolution: str | None, quality: str | None,
                    response_format: str | None, deferred: bool) -> dict:
        _prompt(prompt)
        if n is not None and (not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_IMAGES):
            raise XaiConfigError("n must be an integer from 1 to %d" % MAX_IMAGES)
        _choice(aspect_ratio, IMAGE_ASPECT_RATIOS, "aspect_ratio")
        _choice(resolution, IMAGE_RESOLUTIONS, "resolution")
        _choice(quality, IMAGE_QUALITIES, "quality")
        _choice(response_format, RESPONSE_FORMATS, "response_format")
        if quality is not None and self._image_model != QUALITY_MODEL:
            raise XaiConfigError("quality is only supported for " + QUALITY_MODEL)
        body: dict = {"model": self._image_model, "prompt": prompt}
        for name, value in (("n", n), ("aspect_ratio", aspect_ratio), ("resolution", resolution), ("quality", quality),
                            ("response_format", response_format)):
            if value is not None:
                body[name] = value
        if deferred:
            body["deferred"] = True
        return body

    def generate(self, prompt: str, *, n: int | None = None, aspect_ratio: str | None = None, resolution: str | None = None,
                 quality: str | None = None, response_format: str | None = None) -> ImageBatch:
        """Images from a prompt, answered in the same request. Paid per image: a dropped connection is not repeated."""
        body = self._image_body(prompt, n, aspect_ratio, resolution, quality, response_format, False)
        r = self._http.request("POST", "/images/generations", body, retry_transport=False)
        return self._batch(json_object(r, "image"), body.get("response_format", "url"), n or 1)

    def start_deferred(self, prompt: str, *, n: int | None = None, aspect_ratio: str | None = None, resolution: str | None = None,
                       quality: str | None = None) -> str:
        """Start an image job that finishes in the background and return its request_id; poll_images or wait_images fetches it."""
        body = self._image_body(prompt, n, aspect_ratio, resolution, quality, None, True)
        r = self._http.request("POST", "/images/generations", body, retry_transport=False)
        return self._request_id(json_object(r, "image"))

    def poll_images(self, request_id: str, *, expected: int = MAX_IMAGES) -> Poll:
        """One check of a deferred image job: 202 or status pending is not done; status failed raises; status done carries the batch."""
        rid = _check_request_id(request_id)
        r = self._http.request("GET", "/images/" + rid, accept=(200, 202))
        data = json_object(r, "image poll")
        status = data.get("status")
        if status == "pending":
            return Poll(False)
        if status == "failed":
            raise _job_error(data, "the image job")
        if status != "done":
            raise XaiBadResponseError("HTTP %d but the image poll status is not pending, done or failed" % r.status_code)
        if r.status_code != 200:
            raise XaiBadResponseError("the image job is done but the answer is HTTP %d, not 200" % r.status_code)
        batch = self._batch(data, "url", expected)
        return Poll(True, 100, ImageBatch(batch.images, batch.cost_usd_ticks, rid))

    def wait_images(self, request_id: str, *, expected: int = MAX_IMAGES) -> ImageBatch:
        return self._wait(request_id, lambda: self.poll_images(request_id, expected=expected), "the image job")

    @staticmethod
    def _request_id(data: dict) -> str:
        rid = data.get("request_id")
        if not isinstance(rid, str) or not REQUEST_ID_OK.match(rid):
            raise XaiBadResponseError("HTTP 200 but the answer has no usable request_id")
        return rid

    @staticmethod
    def _batch(data: dict, response_format: str, requested: int) -> ImageBatch:
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise XaiBadResponseError("HTTP 200 but the answer has no images")
        if len(items) > requested:
            raise XaiBadResponseError("HTTP 200 but the answer has more images than were asked for")
        images = []
        for item in items:
            if not isinstance(item, dict):
                raise XaiBadResponseError("HTTP 200 but an image entry is not an object")
            mime = item.get("mime_type")
            mime = mime if isinstance(mime, str) and MIME_OK.match(mime) else None
            if response_format == "b64_json":
                b64 = item.get("b64_json")
                try:
                    if not isinstance(b64, str) or not b64:
                        raise ValueError
                    base64.b64decode(b64, validate=True)
                except (ValueError, binascii.Error):
                    raise XaiBadResponseError("HTTP 200 but an image entry has no valid b64_json") from None
                images.append(GeneratedImage(None, b64, mime))
            else:
                url = item.get("url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise XaiBadResponseError("HTTP 200 but an image entry has no https url")
                images.append(GeneratedImage(url, None, mime))
        return ImageBatch(tuple(images), _ticks(data))

    # ---- video -----------------------------------------------------------------------------------------------------------

    def start_video(self, prompt: str | None = None, *, image_url: str | None = None, duration: int | None = None,
                    aspect_ratio: str | None = None, resolution: str | None = None) -> str:
        """Start a video from a prompt, a still image or both, and return its request_id. Paid: a dropped connection is not repeated."""
        _prompt(prompt, required=image_url is None)
        if image_url is not None and (not isinstance(image_url, str) or not IMAGE_SOURCE_OK.match(image_url)):
            raise XaiConfigError("image_url must be a public URL or a base64 data URI of a jpeg, png or webp image")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool)
                                     or not MIN_SECONDS <= duration <= MAX_SECONDS):
            raise XaiConfigError("duration must be an integer from %d to %d seconds" % (MIN_SECONDS, MAX_SECONDS))
        _choice(aspect_ratio, VIDEO_ASPECT_RATIOS, "aspect_ratio")
        _choice(resolution, VIDEO_RESOLUTIONS, "resolution")
        body: dict = {"model": self._video_model}
        if prompt is not None:
            body["prompt"] = prompt
        if image_url is not None:
            body["image"] = {"url": image_url}
        for name, value in (("duration", duration), ("aspect_ratio", aspect_ratio), ("resolution", resolution)):
            if value is not None:
                body[name] = value
        r = self._http.request("POST", "/videos/generations", body, retry_transport=False)
        return self._request_id(json_object(r, "video"))

    def poll_video(self, request_id: str) -> Poll:
        """One check of a video job: pending carries the progress, failed raises, done carries the VideoResult."""
        rid = _check_request_id(request_id)
        r = self._http.request("GET", "/videos/" + rid, accept=(200, 202))
        data = json_object(r, "video poll")
        status = data.get("status")
        if status == "pending":
            progress = data.get("progress")
            ok = isinstance(progress, int) and not isinstance(progress, bool) and 0 <= progress <= 99
            return Poll(False, progress if ok else None)
        if status == "failed":
            raise _job_error(data, "the video job")
        if status != "done":
            raise XaiBadResponseError("HTTP %d but the video poll status is not pending, done or failed" % r.status_code)
        video = data.get("video")
        if not isinstance(video, dict):
            raise XaiBadResponseError("the video job is done but the answer has no video object")
        if video.get("respect_moderation") is not True:
            if video.get("respect_moderation") is False:
                raise XaiModerationError("the video did not pass moderation, so it has no URL")
            raise XaiBadResponseError("the video job is done but respect_moderation is missing")
        url = video.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise XaiBadResponseError("the video job is done but the video has no https url")
        seconds = video.get("duration")
        seconds = seconds if is_number(seconds) and seconds == int(seconds) and seconds > 0 else None
        return Poll(True, 100, VideoResult(url, None if seconds is None else int(seconds), _ticks(data), rid))

    def wait_video(self, request_id: str) -> VideoResult:
        return self._wait(request_id, lambda: self.poll_video(request_id), "the video job")

    # ---- polling ---------------------------------------------------------------------------------------------------------

    def _wait(self, request_id: str, poll: Callable[[], Poll], what: str) -> Any:
        started = self._clock()
        while True:
            p = poll()
            if p.done:
                return p.result
            if self._clock() - started + self._poll_seconds > self._poll_timeout:
                raise XaiTimeoutError("%s was still pending after %d s (request_id %s)" % (what, int(self._clock() - started), request_id),
                                      request_id=request_id)
            self._sleep(self._poll_seconds)
