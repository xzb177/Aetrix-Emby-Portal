import pytest

from backend.emby_server.scanner import _drive_file_id_from_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://drive.google.com/uc?export=download&id=AAAAbbbbCCCC1111&confirm=t&aexp=1&asig=2",
            "AAAAbbbbCCCC1111",
        ),
        (
            "https://drive.google.com/file/d/DDDDeeeeFFFF2222/view",
            "DDDDeeeeFFFF2222",
        ),
        (
            "https://test-videos.co.uk/v/a.mp4?aexp=1&asig=b",
            "",
        ),
        (
            "https://drive.google.com/uc?export=download&confirm=t",
            "",
        ),
        (
            "",
            "",
        ),
        (
            None,
            "",
        ),
        (
            "https://drive.google.com/uc?id=abc&confirm=t",
            "",
        ),
    ],
)
def test_drive_file_id_from_url(url, expected):
    assert _drive_file_id_from_url(url) == expected


def test_local_dir_files_strm_drive_file_id(tmp_path):
    from backend.emby_server.scanner import _local_dir_files

    (tmp_path / "drive.strm").write_text(
        "https://drive.google.com/uc?export=download&id=AAAAbbbbCCCC1111&confirm=t",
        encoding="utf-8",
    )
    (tmp_path / "other.strm").write_text(
        "https://test-videos.co.uk/v/a.mp4",
        encoding="utf-8",
    )
    (tmp_path / "bad.strm").write_text("not a url", encoding="utf-8")

    failed = []
    results = {sf.name: sf for sf in _local_dir_files(str(tmp_path), failed)}

    assert results["drive.strm"].file_id == "AAAAbbbbCCCC1111"
    assert results["other.strm"].file_id == ""
    assert "bad.strm" not in results
