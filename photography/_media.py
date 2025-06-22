"""
Media type specific behavior.

(I.e. more in-depth stuff we do which is different for photos and videos).
"""

from __future__ import annotations

from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any, Protocol
import subprocess

from attrs import frozen
from PIL import ExifTags, Image
import imagehash

if TYPE_CHECKING:
    from pathlib import Path


class Media(Protocol):
    """
    A photo or video.
    """

    hash: imagehash.ImageHash | None
    metadata_datetime: datetime | None

    @classmethod
    def from_path(cls, path: Path):
        """
        Parse the media at the given path.
        """


@frozen
class Photo:
    """
    A photo.
    """

    _exif: dict[ExifTags, Any]
    #: `imagehash` doesn't cover videos and it's not often I have a cropped or
    #: modified video which isn't otherwise easy to identify via e.g. tilde
    #: naming, but maybe at some point we'll want some video hash
    hash: imagehash.ImageHash

    @cached_property
    def metadata_datetime(self) -> datetime | None:
        exif_date = self._exif.get(ExifTags.Base.DateTimeOriginal)
        if exif_date is not None:
            return datetime.fromisoformat(exif_date)

    @classmethod
    def from_path(cls, path: Path):
        with Image.open(path) as image:
            return cls(exif=image.getexif(), hash=imagehash.phash(image))


@frozen
class Video:
    """
    A video.
    """

    metadata_datetime: datetime | None
    hash = None

    @classmethod
    def from_path(cls, path: Path):
        stdout = subprocess.check_output(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format_tags=creation_time",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            text=True,
        )
        return cls(
            metadata_datetime=(
                datetime.fromisoformat(stdout.strip()) if stdout else None
            ),
        )
