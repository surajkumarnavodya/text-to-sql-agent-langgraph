"""Unit tests for moderation/taxonomy.py and config/moderation_blocklist.py."""

from __future__ import annotations

from pathlib import Path

from config.moderation_blocklist import find_matches, load_blocklist
from moderation.taxonomy import ALL_CATEGORIES, decision_for


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "moderation_blocklist.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestDecisionFor:
    def test_synthetic_media_is_soft_flag(self):
        assert decision_for("synthetic_media") == "soft_flag"

    def test_every_other_category_is_hard_reject(self):
        for category in ALL_CATEGORIES:
            if category == "synthetic_media":
                continue
            assert decision_for(category) == "hard_reject", category


class TestLoadBlocklist:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        assert load_blocklist(tmp_path / "does_not_exist.yaml") == {}

    def test_loads_weapons_and_drugs(self, tmp_path: Path):
        path = _write_yaml(tmp_path, "weapons: [Handgun]\ndrugs: [Cocaine]\n")
        result = load_blocklist(path)
        assert result["weapons"] == ["handgun"]
        assert result["drugs"] == ["cocaine"]

    def test_unrecognized_key_is_skipped_not_raised(self, tmp_path: Path):
        path = _write_yaml(tmp_path, "weapons: [rifle]\nbogus_category: [x]\n")
        result = load_blocklist(path)
        assert "bogus_category" not in result
        assert result["weapons"] == ["rifle"]

    def test_non_list_value_is_skipped(self, tmp_path: Path):
        path = _write_yaml(tmp_path, "weapons: not-a-list\n")
        assert load_blocklist(path) == {}


class TestFindMatches:
    def test_whole_word_case_insensitive_match(self):
        blocklist = {"weapons": ["handgun"]}
        assert find_matches("A HANDGUN was visible on the table", blocklist) == ["weapons"]

    def test_no_match_on_substring(self):
        """"handgun" must not match inside an unrelated word like
        "handgunners" or "shorthandgun" -- whole-word matching only."""
        blocklist = {"weapons": ["handgun"]}
        assert find_matches("shorthandgunmanship", blocklist) == []

    def test_empty_text_returns_empty_list(self):
        assert find_matches("", {"weapons": ["handgun"]}) == []

    def test_multiple_categories_can_match(self):
        blocklist = {"weapons": ["rifle"], "drugs": ["cocaine"]}
        matched = find_matches("a rifle and cocaine were found", blocklist)
        assert set(matched) == {"weapons", "drugs"}
