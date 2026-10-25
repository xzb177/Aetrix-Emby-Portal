from pathlib import Path

import pytest

from backend.emby_server.playback_security import safe_child_name, safe_local_path


def test_local_path_stays_inside_mount_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    movie = root / "movie.mkv"
    movie.write_bytes(b"x")
    assert safe_local_path(str(root), "movie.mkv") == str(movie)


def test_local_path_rejects_parent_traversal(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_local_path(str(root), "../secret.mkv")


def test_local_path_rejects_symlink_escape(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"secret")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        safe_local_path(str(root), "link/secret.mkv")


def test_safe_child_name_accepts_plain_filename(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    assert safe_child_name(str(root), "segment.ts") == str(root / "segment.ts")


@pytest.mark.parametrize("name", ["", ".", "..", "dir/segment.ts", "dir\\segment.ts"])
def test_safe_child_name_rejects_invalid_names(tmp_path, name):
    root = tmp_path / "sandbox"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_child_name(str(root), name)
