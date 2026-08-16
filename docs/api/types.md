# Types API

## FileInfo

::: pydantic_ai_backends.types.FileInfo
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import FileInfo

# Example
file_info: FileInfo = {
    "name": "app.py",
    "path": "/workspace/app.py",
    "is_dir": False,
    "size": 1234,
    "modified_at": "2026-08-16T12:00:00+00:00",  # absent when the backend cannot report one
}
```

## FileData

::: pydantic_ai_backends.types.FileData
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import FileData

# Text: lines, and no encoding key.
file_data: FileData = {
    "content": ["line 1", "line 2", "line 3"],
    "created_at": "2024-01-15T10:30:00Z",
    "modified_at": "2024-01-15T11:00:00Z",
}

# Content that is not valid UTF-8: one base64 entry, marked.
image_data: FileData = {
    "content": ["iVBORw0KGgo..."],
    "created_at": "2024-01-15T10:30:00Z",
    "modified_at": "2024-01-15T11:00:00Z",
    "encoding": "base64",
}
```

A dictionary of these is always a JSON document, which is what lets a host
persist a `StateBackend` and restore it:

```python
import json

stored = json.dumps(backend.files, ensure_ascii=False)
restored = StateBackend(files=json.loads(stored))
```

## WriteResult

::: pydantic_ai_backends.types.WriteResult
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import WriteResult

# Success
result = WriteResult(path="/workspace/app.py")

# Error
result = WriteResult(error="Permission denied")
```

## EditResult

::: pydantic_ai_backends.types.EditResult
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import EditResult

# Success
result = EditResult(path="/workspace/app.py", occurrences=3)

# Error
result = EditResult(error="String not found")
```

## ExecuteResponse

::: pydantic_ai_backends.types.ExecuteResponse
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import ExecuteResponse

# Example
response = ExecuteResponse(
    output="Hello, World!\n",
    exit_code=0,
    truncated=False,
)
```

## GrepMatch

::: pydantic_ai_backends.types.GrepMatch
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import GrepMatch

# Example
match: GrepMatch = {
    "path": "/workspace/app.py",
    "line_number": 42,
    "line": "def hello_world():",
}
```

## RuntimeConfig

::: pydantic_ai_backends.types.RuntimeConfig
    options:
      show_root_heading: true

```python
from pydantic_ai_backends import RuntimeConfig

# Custom runtime
runtime = RuntimeConfig(
    name="ml-env",
    base_image="python:3.12-slim",
    packages=["torch", "transformers"],
    env_vars={"PYTHONUNBUFFERED": "1"},
    work_dir="/workspace",
)
```
