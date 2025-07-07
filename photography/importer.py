"""
A library importer for new media files.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from attrs import field, frozen

if TYPE_CHECKING:
    from collections.abc import Callable


@frozen
class Importer:
    """
    A library importer for new media files.
    """

    library: Path
    quarantine: Path
    move: Callable[[Path, Path], None] = Path.rename

    #: The newest file mtime we'll trust if we only have it as our date
    #: information. Any newer and we assume that we've copied files from a
    #: filesystem that didn't preserve metadata and so it just represents a
    #: copy time.
    _newest_trusted_mtime: datetime = field(
        factory=lambda: datetime.now(UTC) - timedelta(days=3),
    )

    @classmethod
    def dry_run(cls, library: Path, quarantine: Path) -> Importer:
        """
        A dry-run importer doesn't actually move files.

        It still knows how to calculate where files *would* get moved to.
        """
        return cls(
            library=library,
            quarantine=quarantine,
            move=lambda source, destination: None,
        )

    @cached_property
    def trash(self) -> Path:
        """
        The directory where recommended-for-deletion files are moved to.
        """
        trash = self.quarantine / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        return trash

    @cached_property
    def confirm_trash(self) -> Path:
        """
        The directory where manual deletion files are moved to.

        In other words, these are files which we think should be deleted but
        which should be manually looked at first.
        """
        confirm_trash = self.quarantine / "confirm/trash"
        confirm_trash.mkdir(parents=True, exist_ok=True)
        return confirm_trash

    def ingest(self, new_media: Path) -> list[tuple[Path, Path]]:
        """
        Ingest media into the library, quarantining any we're uncertain about.

        Returns a list of all source+destination pairs that were moved.
        """
        mover = Mover(move=self.move)
        for path in new_media.iterdir():
            if not path.is_file():
                continue
            mover.move(path, self._decide(path=path, new_media=new_media))
        return mover.manifest()

    def _decide(self, path: Path, new_media: Path) -> Path:
        """
        Decide where to move the given media file.
        """
        name = path.name

        if name.startswith("."):
            rest = name.lstrip(".")
            if rest == "DS_Store":
                return self.trash / name
            return self.confirm_trash / rest

        # Prefer RAW over JPEG if both exist
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            glob = new_media.glob(f"{path.stem}.dng", case_sensitive=False)
            raws = sum(1 for _ in glob)
            if raws == 1:
                return self.trash / name
            elif raws > 1:
                raise ValueError(f"How does {path} have multiple RAW files?")

            dest = self._library_destination_for(path)
            glob = dest.parent.glob(f"{path.stem}.dng", case_sensitive=False)
            raws = sum(1 for _ in glob)
            if raws == 1:
                return self.trash / name
            elif raws > 1:
                raise ValueError(f"How does {path} have multiple RAW files?")

        # Undocumented Pixel Camera behavior to make `~2.jpg` copies.
        stem, _, tilde = path.stem.rpartition("~")
        if tilde.isdigit():
            original = new_media.joinpath(stem + path.suffix)
            if original.exists():
                return self.trash / name
            maybe = self._library_destination_for(path)
            globs = list(maybe.parent.glob(stem + "*", case_sensitive=False))
            if len(globs) == 1:
                return self.trash / name
            elif globs:
                raise ValueError(
                    f"How does {path} have multiple RAW files? {globs}",
                )

        dest = self._library_destination_for(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def _library_destination_for(self, path: Path) -> Path:
        """
        Compute the expected library destination for a given path.

        If the date can't be parsed, use the confirm-keep quarantine directory.
        """
        name = path.name
        parts = name.split("_")
        if len(parts) < 2 or not parts[1].isdigit():  # noqa: PLR2004
            try:
                stat = path.stat()
            except FileNotFoundError:
                return self.quarantine / name
            else:
                dt = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                if dt > self._newest_trusted_mtime:
                    return self.quarantine / name
                return self.library / f"{dt:%Y/%m/%d}/{name}"

        date_part = parts[1]
        try:
            dt = datetime.strptime(date_part, "%Y%m%d")  # noqa: DTZ007
        except ValueError:
            return self.quarantine / name

        return self.library / f"{dt:%Y/%m/%d}/{name}"


@frozen
class Mover:
    """
    Moves files and keeps a journal of moves.
    """

    _move: Callable[[Path, Path], None]
    _journal: list[tuple[Path, Path]] = field(factory=list)

    def move(self, source: Path, destination: Path) -> None:
        """
        Move a file, recording the move in the journal.
        """
        self._move(source, destination)
        self._journal.append((source.name, destination))

    def manifest(self) -> list[tuple[Path, Path]]:
        """
        Return the manifest of all files moved.
        """
        return self._journal
