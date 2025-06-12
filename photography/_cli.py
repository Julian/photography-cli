"""
A tool for managing photography libraries.

To Do:

  * Move modes:
    - dry-run
    - interactive-confirm
    - symlink
    - really-move

  * Every duplicate-flagged photo better confirm that the "better" one really
    is being imported and not also trashed

  * Show statistics at the end of a run
    - file size
    - number of JPG vs RAW
    - number of videos
    - number of files with each resulting effect (in groups)

  * Build a persistent `imagehash` database
  * Strip leading `.` from hidden files we're trash-confirming
  * Write some manifest out on what files we moved where

  * Signal which files are safe to delete from the phone

  * Ignore `mtime`s before `EARLIEST_YEAR`?

"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import cached_property
from pathlib import Path
from time import strptime
from typing import Any, Protocol
from uuid import UUID
import os
import subprocess

from attrs import field, frozen
from PIL import ExifTags, Image
from rpds import HashTrieMap
import imagehash
import rich_click as click

DESKTOP = (
    Path(os.environ["XDG_DESKTOP_DIR"])
    if "XDG_DESKTOP_DIR" in os.environ
    else Path("~/Desktop").expanduser()
)
QUARANTINE = click.option(
    "--quarantine-into",
    "-Q",
    "quarantine",
    type=click.Path(path_type=Path),
    default=DESKTOP / "quarantined",
    help=(
        "The directory to move quarantined photos into. "
        "It should *not* exist"
        "Nothing should ever be overriden when doing so. "
    ),
)

#: The earliest year we assume we'll ever see a photo from.
EARLIEST_YEAR = 1989

NOW = datetime.now(UTC)


@frozen
class WTF(Exception):
    """
    Something seems off about a file we're dealing with.

    It seems wrong enough to raise an exception and come back and strengthen
    this script.
    """

    path: Path
    description: str


class UnknownImageFormat(Exception):
    """
    We think this is an image but we don't know what format it's in.

    This might mean we need some new parsing logic.
    """


@click.group()
def main() -> None:
    """
    Manage photos and my photography library.
    """


def symlink(media: Path, to: Path):
    to.parent.mkdir(parents=True, exist_ok=True)
    to.symlink_to(media)


def move(media: Path, to: Path):
    to.parent.mkdir(parents=True, exist_ok=True)
    if to.exists():
        raise RuntimeError(to)
    media.rename(to)


@main.command()
@click.argument("jpeg", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def raw(ctx: click.Context, jpeg: Path):
    """
    Try to find a RAW file from a given JPEG.

    Exits unsuccessfully if one isn't found, or if this isn't a JPEG.
    """
    raw = raw_for(jpeg)
    if raw is None:
        click.echo(f"No RAW found for {jpeg}.", err=True)
        ctx.exit(1)
    if not raw.is_file():
        raise WTF(path=raw, description="Somehow this RAW doesn't exist??")
    click.echo(raw)


@main.command(name="import")
@QUARANTINE
@click.argument("new_media", type=click.Path(path_type=Path))
@click.argument("library", type=click.Path(path_type=Path))
def import_(new_media: Path, library: Path, quarantine: Path) -> None:
    """
    Import the new photos/videos from the given directory into the library.

    Quarantine anything we're not sure about into the quarantine directory.
    """
    if quarantine.exists():
        raise click.BadParameter(f"{quarantine} already exists!")

    for directory, _, files in new_media.walk():
        for file in files:
            path = directory / file
            effect = decide(path=path)
            move_to = effect.will_move_to(
                source=path.relative_to(new_media),
                library=library,
                quarantine=quarantine,
            )
            move(media=path, to=move_to)


def decide(path: Path) -> Effect:
    """
    Decide what we want to do with files in this directory.

    It's likely a photo/movie (otherwise we'll say we want to ignore or delete
    it).

    Note that nothing should actually happen from running this function, it
    just decides what we *will* want to do with each file, so it should always
    be made safe to run this and have no side effects.

    Possible outcomes are:

        * `Import`: we know we want this
        * `Trash`: we know this is junk
        * `Duplicated`: this is a photo/video but it duplicates a more original
                        source file which we will import instead

        * `RAWMissingData`: we want this RAW file but it has less data than its
                            corresponding JPEG, so it will be quarantined to
                            decide how to combine the metadata
        * `ManualImport`: we think we want this but don't know where to put it,
                          so it will be quarantined for inspection
        * `ConfirmTrash`: we think we don't need this but are being cautious,
                          so it will be quarantined to confirm
    """
    if path.name == ".DS_Store":
        return Trash()

    if path.name.startswith("."):
        return ConfirmTrash()

    # Undocumented Pixel Camera behavior to make `~2.jpg` images occasionally.
    stem, _, tilde = path.stem.rpartition("~")
    if tilde.isdigit():
        original = path.parent.joinpath(stem + path.suffix)
        if original.is_file():
            return Duplicated(better=original)

        # PXL_FOO.RAW-01.MP.COVER~2.jpg -> PXL_FOO.RAW-02.ORIGINAL.dng
        real_stem, has_raw, rest = stem.rpartition(".RAW-")
        glob = list(path.parent.glob(real_stem + ".RAW-*.dng"))
        if len(glob) == 1:
            return Duplicated(better=glob[0])

        raise WTF(
            path,
            f"{path.name} looks like a tilde-suffixed Google Camera "
            f"file, but {original} doesn't exist alongside it. "
            "Perhaps we should quarantine these files for manual "
            "inspection to decide if they should be imported as "
            "the best we've got, though it would be good to understand "
            "when it's possible for this to happen.",
        )

    # Similar for other Pixel processed files like long exposure which get
    # named like PXL_20240108_043740102.LONG_EXPOSURE-01.COVER.jpg
    original = path.parent / path.name.replace("01.COVER", "02.ORIGINAL")
    if original != path and original.is_file():
        return Duplicated(better=original)

    match path.suffix:
        case ".mp4" | ".mov":
            media = Video.from_path(path)
        case ".jpg" | ".jpeg":
            raw_path = raw_for(path)
            if raw_path is not None:
                with Image.open(raw_path) as raw, Image.open(path) as jpg:
                    jpg_gps = jpg.getexif().get_ifd(ExifTags.Base.GPSInfo)
                    raw_gps = raw.getexif().get_ifd(ExifTags.Base.GPSInfo)
                    missing = {
                        ExifTags.GPSTAGS[k]: jpg_gps[k]
                        for k in jpg_gps.keys() - raw_gps.keys()
                        # these occasionally seem missing from RAW?
                        # and I guess who cares
                        if k
                        not in {
                            ExifTags.GPS.GPSImgDirection,
                            ExifTags.GPS.GPSImgDirectionRef,
                            ExifTags.GPS.GPSVersionID,
                        }
                    }
                    if missing:
                        return RAWMissingData(jpeg=path)
                return Duplicated(better=raw_path)
            media = Photo.from_path(path)
        case ".dng":
            media = Photo.from_path(path)
        case extension:
            raise WTF(path, f"We haven't yet handled {extension} files.")

    return Import.if_dates_match(path=path, media=media)


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


class Effect(Protocol):
    """
    An effect we can perform on a file path.

    We represent them explicitly in order to support dry-running, filtering
    which we perform, etc.
    """

    #: Whether this effect means we won't immediately import this photo/video.
    problematic: bool

    def will_move_to(
        self,
        source: Path,
        library: Path,
        quarantine: Path,
    ) -> Path:
        """
        Where will this effect will move the source file to?
        """


@frozen
class Trash:
    """
    A file which we will delete.

    It's known to be useless.
    """

    problematic = True

    def will_move_to(self, source: Path, library: Path, quarantine: Path):
        return quarantine / "trash" / source.name


@frozen
class Duplicated:
    """
    Something we'll delete because it's duplicated.

    But it's at least a photo/video file, so we might double check we really
    are deleting something worse than what it duplicates.
    """

    problematic = True

    #: The path to the better version of this image
    # TODO: we better be importing this, so that should be represented somehow.
    better: Path = field(repr=str)

    def will_move_to(self, source: Path, library: Path, quarantine: Path):
        #       * Check the photo is worse than the original before deleting?
        return quarantine / "trash" / source.name


@frozen
class ManualImport:
    """
    A file which we'll quarantine for manual importing.

    It seems likely to be a photo/video we want, but we can't figure out when
    it was taken, or it seems to have conflicting metadata.
    """

    problematic = True

    #: Discrepant data which caused us to say this needs manual inspection.
    #: Or, empty if the reason was *no* data.
    reason: HashTrieMap[str, Any] = field(default=HashTrieMap(), repr=dict)

    def will_move_to(self, source: Path, library: Path, quarantine: Path):
        return quarantine / "keep" / source.name


@frozen
class RAWMissingData:
    """
    A file which we want to import but which is missing data.

    This means we have a RAW and a JPEG, where we want to import the RAW, but
    the JPEG has EXIF data which hasn't made it into the RAW.

    There's of course no information on why this happened unfortunately.

    The resolution should be to merge the EXIF data into the RAW.

    Perhaps in the future we'll automatically copy EXIF data.
    """

    problematic = True

    # TODO: Make this `ProcessedPhoto`?
    jpeg: Path = field(repr=str)

    def will_move_to(self, source: Path, library: Path, quarantine: Path):
        # XXX: We need the JPEG moved there too
        return quarantine.joinpath(
            "confirm/keep/exif-pairs",
            source.stem,
            source.name,
        )


@frozen
class ConfirmTrash:
    """
    A file which we will quarantine for verification.

    It seems likely to be deletable, but we'll let a human decide.
    """

    problematic = True

    def will_move_to(self, source: Path, library: Path, quarantine: Path):
        """
        Strip leading `.` to make these easier to review.
        """
        return quarantine / "confirm/trash" / source.name.lstrip(".")


def datetime_from(maybe_ymd: str, maybe_time_and_rest: str) -> datetime | None:
    """
    Parse a datetime out of the PXL_-style file format's date/time components.
    """
    maybe_time, _, rest = maybe_time_and_rest.partition(".")
    try:
        struct = strptime(f"{maybe_ymd}_{maybe_time}", "%Y%m%d_%H%M%S%f")
    except ValueError:
        try:
            struct = strptime(maybe_ymd, "%Y%m%d")
        except ValueError:
            return

    from_path = datetime(*struct[0:6], tzinfo=UTC)
    if from_path.year >= EARLIEST_YEAR and from_path <= NOW:
        return from_path


@frozen
class Import:
    """
    A photo/video we'll import.

    It seems likely to be new to the library!
    """

    problematic = False

    date: date

    @classmethod
    def if_dates_match(cls, path: Path, media: Media) -> Import | ManualImport:
        """
        Import if the `mtime`, file path and metadata dates match.

        Otherwise (or if all 3 seem wrong) manually import.
        """
        from_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        # if the mtime is in the past few days (or in the future), ignore it
        # as probably we have media whose mtime has been lost and set to when
        # we copied the files from whatever broken filesystem they came from
        if from_mtime > (NOW - timedelta(days=3)):
            from_mtime = None

        from_path = None

        match path.stem.replace("-", "_").split("_", 2):
            case ["PXL" | "IMG" | "VID", ymd, rest]:
                from_path = datetime_from(ymd, rest)
            case _ if path.stem[0].isdigit() or _is_uuid(path.stem):
                return ManualImport()
            case _:
                # TODO: There's more cases we should handle here, e.g. for DSC.
                raise WTF(path, "Implement me for other prefixes!")

        from_metadata = media.metadata_datetime

        match sorted(
            each
            for each in [from_mtime, from_path, from_metadata]
            if each is not None
        ):
            case [date]:
                return cls(date=date)
            case [earliest, *_, latest]:
                # Trust path dates a lot
                if earliest == from_path:
                    return cls(date=from_path)

                # allow for 24 hours of difference because we're not taking
                # time into account, and somewhere has probably introduced
                # naive datetimes
                if latest - earliest > timedelta(days=1):
                    return ManualImport(
                        reason=HashTrieMap(
                            mtime=from_mtime,
                            path=from_path,
                            metadata=from_metadata,
                        ),
                    )
                return cls(date=earliest)
            case []:
                raise WTF(path, "No dates??")

    def will_move_to(self, source: Path, library: Path, quarantine: Path):
        return library / self.date.strftime("%Y/%m/%d") / source.name


def raw_for(path: Path) -> Path | None:
    """
    Find the raw file for this (JPEG) image.

    So only call me with JPEGs.
    """
    if path.suffix not in {".jpg", ".jpeg"}:
        raise WTF(path, "We're looking for the RAW file for a non-JPEG!")

    superstem, _, _ = path.name.partition(".")
    match list(path.parent.glob(f"{superstem}*.dng")):  # TODO: other RAW exts
        case []:
            return
        case [raw]:
            return raw
        case raws:
            raise WTF(
                path,
                "We seem to have more than one raw file for the same image! "
                f"Found: {raws}",
            )


def _is_uuid(s: str) -> bool:
    try:
        UUID(s)
    except ValueError:
        return False
    return True
