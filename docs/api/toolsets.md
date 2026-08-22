# Toolsets API

## create_console_toolset

::: pydantic_ai_backends.toolsets.console.create_console_toolset
    options:
      show_root_heading: true

## get_console_system_prompt

::: pydantic_ai_backends.toolsets.console.get_console_system_prompt
    options:
      show_root_heading: true

## ConsoleDeps

::: pydantic_ai_backends.toolsets.console.ConsoleDeps
    options:
      show_root_heading: true

## ToolText

::: pydantic_ai_backends.toolsets.descriptions.ToolText
    options:
      show_root_heading: true

## Console Tools

The toolset registers these tools. What each one *says* — its description and the
text describing every argument — is not written beside the function: it lives in
`TOOL_TEXT`, keyed by tool name, and is assigned when the tool is registered. Read
`TOOL_TEXT["grep"].render()` to see exactly what the model is handed.

```python
async def ls(ctx, path: str = ".") -> str: ...
async def read_file(ctx, path: str, offset: int = 0, limit: int = 2000) -> str: ...
async def write_file(ctx, path: str, content: str) -> str: ...
async def edit_file(
    ctx, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str: ...
async def hashline_edit(
    ctx,
    path: str,
    start_line: int,
    start_hash: str,
    new_content: str,
    end_line: int | None = None,
    end_hash: str | None = None,
    insert_after: bool = False,
) -> str: ...
async def glob(ctx, pattern: str, path: str = ".") -> str: ...
async def grep(
    ctx,
    pattern: str,
    path: str | None = None,
    glob_pattern: str | None = None,
    output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches",
    ignore_hidden: bool = True,
) -> str: ...
async def execute(ctx, command: str, timeout: int | None = 120) -> str: ...
async def run_in_background(ctx, command: str) -> str: ...
async def read_output(ctx, shell_id: str) -> str: ...
async def kill_shell(ctx, shell_id: str) -> str: ...
async def list_shells(ctx) -> str: ...
```
