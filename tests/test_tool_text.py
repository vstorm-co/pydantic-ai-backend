"""Tests for the text the model reads about each console tool."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import UserError

from pydantic_ai_backends import create_console_toolset
from pydantic_ai_backends.toolsets import descriptions as text_module
from pydantic_ai_backends.toolsets.descriptions import OVERRIDE_KEYS, TOOL_TEXT, ToolText


def _text_id(tool_name: str, edit_format: str) -> str:
    """Which `TOOL_TEXT` entry a registered tool takes its text from."""
    if tool_name == "read_file" and edit_format == "hashline":
        return "hashline_read_file"
    return tool_name


class TestEveryToolIsDescribed:
    """What the model is handed, for every tool, in both edit formats."""

    @pytest.mark.parametrize("edit_format", ["str_replace", "hashline"])
    def test_every_argument_carries_its_own_description(self, edit_format: str) -> None:
        """An argument with no description is one the model has to guess at."""
        toolset = create_console_toolset(edit_format=edit_format)  # type: ignore[arg-type]

        undescribed = {
            f"{name}.{arg}": schema
            for name, tool in toolset.tools.items()
            for arg, schema in tool.tool_def.parameters_json_schema.get("properties", {}).items()
            if not schema.get("description")
        }

        assert undescribed == {}

    @pytest.mark.parametrize("edit_format", ["str_replace", "hashline"])
    def test_the_argument_text_matches_the_signature(self, edit_format: str) -> None:
        """`TOOL_TEXT` names every argument the tool takes, and no others.

        Both directions, because both fail silently: an argument the registry
        forgot reaches the model undescribed, and one it names that no longer
        exists is text nobody will ever see and nobody will notice is stale.
        """
        toolset = create_console_toolset(edit_format=edit_format)  # type: ignore[arg-type]

        for name, tool in toolset.tools.items():
            declared = set(TOOL_TEXT[_text_id(name, edit_format)].args)
            actual = set(tool.tool_def.parameters_json_schema.get("properties", {}))
            assert declared == actual, name

    @pytest.mark.parametrize("edit_format", ["str_replace", "hashline"])
    def test_the_description_is_the_rendered_text(self, edit_format: str) -> None:
        toolset = create_console_toolset(edit_format=edit_format)  # type: ignore[arg-type]

        for name, tool in toolset.tools.items():
            expected = TOOL_TEXT[_text_id(name, edit_format)].render()
            assert tool.tool_def.description == expected

    def test_every_tool_says_what_it_returns(self) -> None:
        """The half a tool description usually leaves out."""
        assert all(entry.returns for entry in TOOL_TEXT.values())

    def test_the_exported_constants_render_the_same_objects(self) -> None:
        """A caller importing `LS_DESCRIPTION` reads what the model reads."""
        assert TOOL_TEXT["ls"].render() == text_module.LS_DESCRIPTION
        assert TOOL_TEXT["execute"].render() == text_module.EXECUTE_DESCRIPTION
        assert (
            TOOL_TEXT["hashline_read_file"].render()
        ) == text_module.HASHLINE_READ_FILE_DESCRIPTION


class TestProfiles:
    def test_the_agent_profile_drops_the_repository_guidance(self) -> None:
        """An agent with a scratch workspace pays for none of it."""
        execute = TOOL_TEXT["execute"]

        agent = execute.render("agent")
        coding = execute.render("coding")

        assert execute.coding in coding
        assert execute.coding not in agent
        assert execute.summary in agent
        assert len(agent) < len(coding)

    def test_the_profile_reaches_the_registered_tool(self) -> None:
        lean = create_console_toolset(profile="agent")
        full = create_console_toolset(profile="coding")

        assert lean.tools["execute"].tool_def.description == TOOL_TEXT["execute"].render("agent")
        assert full.tools["execute"].tool_def.description == TOOL_TEXT["execute"].render("coding")

    def test_a_text_with_only_a_summary_renders_it_alone(self) -> None:
        assert ToolText(summary="Do one thing.").render() == "Do one thing."


class TestOverrides:
    def test_a_string_replaces_the_description_and_keeps_the_arguments(self) -> None:
        """A host rewording a tool should not lose its argument text with it."""
        toolset = create_console_toolset(descriptions={"execute": "Run a command."})

        execute = toolset.tools["execute"].tool_def
        assert execute.description == "Run a command."
        assert execute.parameters_json_schema["properties"]["command"]["description"]

    def test_a_tool_text_replaces_both_halves(self) -> None:
        replacement = ToolText(
            summary="Run a command in the customer's environment.",
            args={
                "command": "The command, exactly as the runbook writes it.",
                "timeout": "Seconds.",
            },
        )

        toolset = create_console_toolset(descriptions={"execute": replacement})

        execute = toolset.tools["execute"].tool_def
        assert execute.description == replacement.render()
        assert execute.parameters_json_schema["properties"]["command"]["description"] == (
            "The command, exactly as the runbook writes it."
        )

    def test_an_unknown_tool_name_is_refused(self) -> None:
        """Silently ignoring it is how an override reaches nothing for months."""
        with pytest.raises(UserError) as exc:
            create_console_toolset(descriptions={"read_flie": "..."})

        assert "read_flie" in str(exc.value)

    def test_the_read_file_override_covers_both_edit_formats(self) -> None:
        """One tool, two texts, and a caller who names the tool it sees."""
        toolset = create_console_toolset(
            edit_format="hashline", descriptions={"read_file": "Read a file."}
        )

        assert toolset.tools["read_file"].tool_def.description == "Read a file."

    def test_the_override_keys_are_the_tool_names(self) -> None:
        assert "hashline_read_file" not in OVERRIDE_KEYS
        assert set(TOOL_TEXT) - {"hashline_read_file"} == OVERRIDE_KEYS


class TestGeneratedDocstring:
    def test_arguments_are_listed_under_a_google_args_section(self) -> None:
        text = ToolText(summary="Do a thing.", args={"path": "Where."})

        assert text.docstring() == "Do a thing.\n\nArgs:\n    path: Where."

    def test_a_tool_with_no_arguments_gets_no_args_section(self) -> None:
        assert ToolText(summary="Do a thing.").docstring() == "Do a thing."
