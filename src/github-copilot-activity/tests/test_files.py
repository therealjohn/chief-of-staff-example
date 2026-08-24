from __future__ import annotations

import files


def test_shared_file_directory_hashes_the_conversation_id(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_FILES_DIR", tmp_path)

    path = files._conv_dir("tenant/user/private-conversation")

    assert path.parent == tmp_path
    assert len(path.name) == 64
    assert "tenant" not in path.name
    assert "private" not in path.name
