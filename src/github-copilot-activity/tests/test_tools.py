from __future__ import annotations

import pytest
from copilot import ToolInvocation

import tools


def test_deliver_file_is_only_for_explicit_download_requests():
    deliver_file = next(
        tool
        for tool in tools.build_tools(
            "conversation",
            include_file_delivery=True,
        )
        if tool.name == "deliver_file"
    )
    description = deliver_file.description.lower()

    assert "explicitly asks for a downloadable file" in description
    assert "do not use" in description
    assert "briefing" in description


def test_file_delivery_is_disabled_by_default():
    names = [tool.name for tool in tools.build_tools("conversation")]

    assert "deliver_file" not in names


def test_task_store_filename_hashes_the_conversation_id(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_STORE_DIR", tmp_path)

    path = tools._task_file("tenant/user/private-conversation")

    assert path.parent == tmp_path
    assert path.suffix == ".json"
    assert len(path.stem) == 64
    assert "tenant" not in path.name
    assert "private" not in path.name


@pytest.mark.asyncio
async def test_add_task_tool_rejects_blank_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_STORE_DIR", tmp_path)
    add_task = next(
        tool for tool in tools.build_tools("conversation") if tool.name == "add_task"
    )

    result = await add_task.handler(
        ToolInvocation(arguments={"title": "   "})
    )

    assert "blank" in result.text_result_for_llm.lower()
    assert tools.get_tasks("conversation") == []


@pytest.mark.asyncio
async def test_direct_and_model_task_paths_share_deduplication(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_STORE_DIR", tmp_path)
    add_task = next(
        tool for tool in tools.build_tools("conversation") if tool.name == "add_task"
    )
    direct = tools.add_task_direct("conversation", "Prepare briefing")

    result = await add_task.handler(
        ToolInvocation(arguments={"title": "prepare BRIEFING"})
    )

    assert direct is not None
    assert "already" in result.text_result_for_llm.lower()
    assert len(tools.get_tasks("conversation")) == 1
