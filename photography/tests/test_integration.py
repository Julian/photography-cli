from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import mktime
from uuid import uuid4
import os

import pytest

from photography.importer import Importer


def tree(dir: Path):
    return {
        each: each.read_text()
        for each in dir.rglob("*") if each.is_file()
    }


def create(new_media_dir: Path, *names: str) -> dict[str, str]:
    """
    Create files with unique contents in new_media_dir for each given name.

    Returns a dict mapping each filename to its unique content.
    """
    assert not tree(new_media_dir), "Media directory must be empty"
    contents = {}
    for name in names:
        contents[name] = arbitrary_unique = f"{name}:{uuid4()}"
        with new_media_dir.joinpath(name).open("x") as file:
            file.write(arbitrary_unique)
    return contents


@pytest.fixture
def importer(tmp_path):
    library = tmp_path / "library"
    library.mkdir()

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()

    return Importer(library=library, quarantine=quarantine)


def test_prefers_raw_over_jpeg(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20240101_123456789.DNG",
        "PXL_20240101_123456789.JPG",
    )
    manifest = importer.ingest(tmp_path)
    lib = importer.library
    quarantine = importer.quarantine

    assert (tree(lib), tree(quarantine)) == (
        {
            lib / "2024/01/01/PXL_20240101_123456789.DNG": contents["PXL_20240101_123456789.DNG"],
        },
        {
            importer.trash / "PXL_20240101_123456789.JPG": contents["PXL_20240101_123456789.JPG"],
        },
    )
    assert manifest == [
        (
            "PXL_20240101_123456789.DNG",
            lib / "2024/01/01/PXL_20240101_123456789.DNG",
        ),
        (
            "PXL_20240101_123456789.JPG",
            importer.trash / "PXL_20240101_123456789.JPG",
        ),
    ]


def test_all_unique(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20250101_151542487.RAW-02.ORIGINAL.dng",
        "PXL_20231231_235959999.JPG",
        "PXL_20240115_101010101.MP4",
    )

    manifest = importer.ingest(tmp_path)

    lib = importer.library
    assert (tree(lib), tree(importer.quarantine)) == (
        {
            lib / "2025/01/01/PXL_20250101_151542487.RAW-02.ORIGINAL.dng": contents["PXL_20250101_151542487.RAW-02.ORIGINAL.dng"],
            lib / "2023/12/31/PXL_20231231_235959999.JPG": contents["PXL_20231231_235959999.JPG"],
            lib / "2024/01/15/PXL_20240115_101010101.MP4": contents["PXL_20240115_101010101.MP4"],
        },
        {},
    )
    assert sorted(manifest) == [
        (
            "PXL_20231231_235959999.JPG",
            lib / "2023/12/31/PXL_20231231_235959999.JPG",
        ),
        (
            "PXL_20240115_101010101.MP4",
            lib / "2024/01/15/PXL_20240115_101010101.MP4",
        ),
        (
            "PXL_20250101_151542487.RAW-02.ORIGINAL.dng",
            lib / "2025/01/01/PXL_20250101_151542487.RAW-02.ORIGINAL.dng",
        ),
    ]


def test_no_filename_date_but_valid_mtime(importer, tmp_path):
    filename = "DSC_image.JPG"
    contents = create(tmp_path, filename)
    file_path = tmp_path / filename

    jan_2_2024 = mktime((2024, 1, 2, 12, 0, 0, 0, 0, -1))
    os.utime(file_path, (jan_2_2024, jan_2_2024))

    manifest = importer.ingest(tmp_path)
    lib = importer.library

    expected_dest = lib / "2024/01/02/DSC_image.JPG"
    assert (tree(lib), tree(importer.quarantine)) == (
        {
            expected_dest: contents[filename],
        },
        {},
    )
    assert manifest == [
        (filename, expected_dest),
    ]


def test_quarantine_mtime_in_future(importer, tmp_path):
    filename = "DSC_future.JPG"
    contents = create(tmp_path, filename)
    file_path = tmp_path / filename

    future_time = mktime(
        (datetime.now(UTC).year + 10, 1, 1, 12, 0, 0, 0, 0, -1),
    )
    os.utime(file_path, (future_time, future_time))

    manifest = importer.ingest(tmp_path)
    quarantine = importer.quarantine

    assert (tree(importer.library), tree(quarantine)) == (
        {},
        {
            quarantine / filename: contents[filename],
        },
    )
    assert manifest == [
        (filename, quarantine / filename),
    ]


def test_quarantine_if_mtime_too_recent(importer, tmp_path):
    filename = "DSC_recent.JPG"
    contents = create(tmp_path, filename)
    file_path = tmp_path / filename

    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    too_recent = mktime(
        (yesterday.year, yesterday.month, yesterday.day, 12, 0, 0, 0, 0, -1),
    )
    os.utime(file_path, (too_recent, too_recent))

    manifest = importer.ingest(tmp_path)
    quarantine = importer.quarantine

    assert (tree(importer.library), tree(quarantine)) == (
        {},
        {
            quarantine / filename: contents[filename],
        },
    )
    assert manifest == [
        (filename, quarantine / filename),
    ]


def test_custom_newest_trusted_mtime(tmp_path):
    now = datetime.now(UTC)
    importer = Importer(
        library=tmp_path / "library",
        quarantine=tmp_path / "quarantine",
        newest_trusted_mtime=now + timedelta(minutes=5),
    )

    new = tmp_path / "new"
    new.mkdir()

    filename = "DSC_new.JPG"
    contents = create(new, filename)

    manifest = importer.ingest(new)
    lib = importer.library

    expected_dest = lib / f"{now:%Y/%m/%d}/{filename}"
    assert (tree(lib), tree(importer.quarantine)) == (
        {
            expected_dest: contents[filename],
        },
        {},
    )
    assert manifest == [
        (filename, expected_dest),
    ]


def test_quarantine_invalid_date(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20231301_151542487.JPG",  # Invalid month 13
    )

    manifest = importer.ingest(tmp_path)

    lib = importer.library
    quarantine = importer.quarantine
    assert (tree(lib), tree(quarantine)) == (
        {},
        {
            quarantine / "PXL_20231301_151542487.JPG": contents["PXL_20231301_151542487.JPG"],
        },
    )
    assert manifest == [
        (
            "PXL_20231301_151542487.JPG",
            quarantine / "PXL_20231301_151542487.JPG",
        ),
    ]


def test_quarantine_no_date_info(importer, tmp_path):
    contents = create(
        tmp_path,
        "IMG_foo_bar.jpg",
        "12345678.jpg",
    )

    manifest = importer.ingest(tmp_path)
    quarantine = importer.quarantine

    assert (tree(importer.library), tree(quarantine)) == (
        {},
        {
            quarantine / "IMG_foo_bar.jpg": contents["IMG_foo_bar.jpg"],
            quarantine / "12345678.jpg": contents["12345678.jpg"],
        },
    )
    assert manifest == [
        ("12345678.jpg", quarantine / "12345678.jpg"),
        ("IMG_foo_bar.jpg", quarantine / "IMG_foo_bar.jpg"),
    ]


def test_quarantine_numeric_and_uuid(importer, tmp_path):
    contents = create(
        tmp_path,
        "12345678.jpg",
        "550e8400-e29b-41d4-a716-446655440000.jpg",
    )

    manifest = importer.ingest(tmp_path)
    quarantine = importer.quarantine

    assert (tree(importer.library), tree(quarantine)) == (
        {},
        {
            quarantine / "12345678.jpg": contents["12345678.jpg"],
            quarantine / "550e8400-e29b-41d4-a716-446655440000.jpg": contents["550e8400-e29b-41d4-a716-446655440000.jpg"],
        },
    )
    assert sorted(manifest) == [
        ("12345678.jpg", quarantine / "12345678.jpg"),
        ("550e8400-e29b-41d4-a716-446655440000.jpg", quarantine / "550e8400-e29b-41d4-a716-446655440000.jpg"),
    ]


def test_trashes_junk(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20240101_123456789.JPG",
        ".DS_Store",
    )

    manifest = importer.ingest(tmp_path)

    lib = importer.library
    quarantine = importer.quarantine

    assert (tree(lib), tree(quarantine)) == (
        {
            lib / "2024/01/01/PXL_20240101_123456789.JPG": contents["PXL_20240101_123456789.JPG"],
        },
        {
            importer.trash / ".DS_Store": contents[".DS_Store"],
        },
    )
    assert manifest == [
        (".DS_Store", importer.trash / ".DS_Store"),
        (
            "PXL_20240101_123456789.JPG",
            lib / "2024/01/01/PXL_20240101_123456789.JPG",
        ),
    ]


def test_quarantines_other_dotfile(importer, tmp_path):
    contents = create(tmp_path, ".hiddenfile.jpg")

    manifest = importer.ingest(tmp_path)

    lib = importer.library
    confirm_trash = importer.confirm_trash

    assert (tree(lib), tree(importer.quarantine)) == (
        {},
        {
            confirm_trash / "hiddenfile.jpg": contents[".hiddenfile.jpg"],
        },
    )
    assert manifest == [
        (".hiddenfile.jpg", confirm_trash / "hiddenfile.jpg"),
    ]


def test_duplicate_jpeg_is_trashed(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20240101_123456789.JPG",
        "PXL_20240101_123456789~2.JPG",
    )
    manifest = importer.ingest(tmp_path)
    lib = importer.library

    assert (tree(lib), tree(importer.quarantine)) == (
        {
            lib / "2024/01/01/PXL_20240101_123456789.JPG": contents["PXL_20240101_123456789.JPG"],
        },
        {
            importer.trash / "PXL_20240101_123456789~2.JPG": contents["PXL_20240101_123456789~2.JPG"],
        },
    )
    assert manifest == [
        (
            "PXL_20240101_123456789.JPG",
            lib / "2024/01/01/PXL_20240101_123456789.JPG",
        ),
        (
            "PXL_20240101_123456789~2.JPG",
            importer.trash / "PXL_20240101_123456789~2.JPG",
        ),
    ]


def test_multiple_duplicates(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20240101_123456789.DNG",
        "PXL_20240101_123456789.JPG",
        "PXL_20240101_123456789~2.JPG",
        "PXL_20240101_123456789~3.JPG",
    )

    manifest = importer.ingest(tmp_path)
    lib = importer.library
    quarantine = importer.quarantine

    assert (tree(lib), tree(quarantine)) == (
        {
            lib / "2024/01/01/PXL_20240101_123456789.DNG": contents["PXL_20240101_123456789.DNG"],
        },
        {
            importer.trash / "PXL_20240101_123456789.JPG": contents["PXL_20240101_123456789.JPG"],
            importer.trash / "PXL_20240101_123456789~2.JPG": contents["PXL_20240101_123456789~2.JPG"],
            importer.trash / "PXL_20240101_123456789~3.JPG": contents["PXL_20240101_123456789~3.JPG"],
        },
    )
    assert sorted(manifest) == [
        (
            "PXL_20240101_123456789.DNG",
            lib / "2024/01/01/PXL_20240101_123456789.DNG",
        ),
        (
            "PXL_20240101_123456789.JPG",
            importer.trash / "PXL_20240101_123456789.JPG",
        ),
        (
            "PXL_20240101_123456789~2.JPG",
            importer.trash / "PXL_20240101_123456789~2.JPG",
        ),
        (
            "PXL_20240101_123456789~3.JPG",
            importer.trash / "PXL_20240101_123456789~3.JPG",
        ),
    ]


def test_tilde_jpeg_with_dng_already_in_library(importer, tmp_path):
    contents = create(
        tmp_path,
        "PXL_20240101_123456789~2.JPG",
    )

    lib = importer.library

    raw_path = lib / "2024/01/01/PXL_20240101_123456789.DNG"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_content = "RAW:already-in-library"
    raw_path.write_text(raw_content)

    manifest = importer.ingest(tmp_path)
    quarantine = importer.quarantine

    assert (tree(lib), tree(quarantine)) == (
        {
            raw_path: raw_content,
        },
        {
            importer.trash / "PXL_20240101_123456789~2.JPG": contents["PXL_20240101_123456789~2.JPG"],
        },
    )
    assert manifest == [
        (
            "PXL_20240101_123456789~2.JPG",
            importer.trash / "PXL_20240101_123456789~2.JPG",
        ),
    ]


def test_dry_run(tmp_path):
    new = tmp_path / "new"
    new.mkdir()

    library = tmp_path / "library"
    library.mkdir()

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()

    contents = create(
        new,
        "PXL_20240101_123456789.DNG",
        "PXL_20240101_123456789.JPG",
        ".DS_Store",
        "IMG_foo_bar.jpg",
        "PXL_20240101_999999999.JPG",
        "PXL_20240101_999999999~2.JPG",
        "PXL_20240102_111111111.MP4",
    )

    dry_run = Importer.dry_run(library=library, quarantine=quarantine)

    would_do = dry_run.ingest(new)

    # We didn't really do anything.
    assert tree(tmp_path) == {
        new / each: contents[each] for each in [
            "PXL_20240101_123456789.DNG",
            "PXL_20240101_123456789.JPG",
            ".DS_Store",
            "IMG_foo_bar.jpg",
            "PXL_20240101_999999999.JPG",
            "PXL_20240101_999999999~2.JPG",
            "PXL_20240102_111111111.MP4",
        ]
    }

    real = Importer(library=library, quarantine=quarantine).ingest(new)

    assert would_do == real

    assert (tree(library), tree(quarantine)) == (
        {
            library / "2024/01/01/PXL_20240101_123456789.DNG": contents["PXL_20240101_123456789.DNG"],
            library / "2024/01/01/PXL_20240101_999999999.JPG": contents["PXL_20240101_999999999.JPG"],
            library / "2024/01/02/PXL_20240102_111111111.MP4": contents["PXL_20240102_111111111.MP4"],
        },
        {
            quarantine / "trash/.DS_Store": contents[".DS_Store"],
            quarantine / "trash/PXL_20240101_123456789.JPG": contents["PXL_20240101_123456789.JPG"],
            quarantine / "trash/PXL_20240101_999999999~2.JPG": contents["PXL_20240101_999999999~2.JPG"],
            quarantine / "IMG_foo_bar.jpg": contents["IMG_foo_bar.jpg"],
        },
    )
