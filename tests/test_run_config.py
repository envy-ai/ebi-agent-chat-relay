"""Tests for RunConfig validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from claude_discord.claude.types import ImageData
from claude_discord.cogs.run_config import RunConfig


def _make_config(**overrides):
    """Create a RunConfig with minimal required fields."""
    defaults = {
        "thread": MagicMock(),
        "runner": MagicMock(),
        "prompt": "hello",
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


_SAMPLE_IMAGE = ImageData(data="aGVsbG8=", media_type="image/png")


class TestRunConfigValidation:
    """Test RunConfig.__post_init__ validation."""

    def test_empty_prompt_no_images_raises(self):
        """Empty prompt without images should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            _make_config(prompt="")

    def test_empty_prompt_with_images_allowed(self):
        """Empty prompt with images should be accepted."""
        config = _make_config(prompt="", images=[_SAMPLE_IMAGE])
        assert config.prompt == ""
        assert config.images == [_SAMPLE_IMAGE]

    def test_nonempty_prompt_no_images_allowed(self):
        """Normal text prompt should work as before."""
        config = _make_config(prompt="hello")
        assert config.prompt == "hello"

    def test_nonempty_prompt_with_images_allowed(self):
        """Text prompt with images should work."""
        config = _make_config(prompt="describe this", images=[_SAMPLE_IMAGE])
        assert config.prompt == "describe this"

    def test_empty_prompt_empty_images_raises(self):
        """Empty prompt with empty image list should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            _make_config(prompt="", images=[])

    def test_with_prompt_preserves_images(self):
        """with_prompt should carry over images."""
        original = _make_config(prompt="old", images=[_SAMPLE_IMAGE])
        updated = original.with_prompt("new prompt")
        assert updated.prompt == "new prompt"
        assert updated.images == [_SAMPLE_IMAGE]


class TestPromptPolicyDefaults:
    def test_optional_prompt_policies_default_off(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = _make_config()

        assert config.lounge_prompt_enabled is False
        assert config.post_compact_guardrail_enabled is False
        assert config.worktree_prompt_enabled is False

    def test_optional_prompt_policies_can_be_enabled_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CCDB_LOUNGE_PROMPT_ENABLED": "true",
                "CCDB_POST_COMPACT_GUARDRAIL_ENABLED": "1",
                "CCDB_WORKTREE_PROMPT_ENABLED": "yes",
            },
            clear=True,
        ):
            config = _make_config()

        assert config.lounge_prompt_enabled is True
        assert config.post_compact_guardrail_enabled is True
        assert config.worktree_prompt_enabled is True
