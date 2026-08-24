from __future__ import annotations

from activity_values import as_dict


class _ModelValue:
    def model_dump(self, *, by_alias: bool):
        assert by_alias is True
        return {"aliased": "value"}


def test_as_dict_returns_mapping_unchanged():
    value = {"key": "value"}

    assert as_dict(value) is value


def test_as_dict_dumps_sdk_models_with_aliases():
    assert as_dict(_ModelValue()) == {"aliased": "value"}


def test_as_dict_returns_empty_mapping_for_unsupported_values():
    assert as_dict(None) == {}
    assert as_dict("not a mapping") == {}
