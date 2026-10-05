# Local CLI Agent Example

A full-featured CLI coding assistant: the console tools in Pydantic AI's `LocalWorkspace`.

## Use Case

- Personal CLI coding assistants
- Development tools
- Single-user applications
- Scripts that need AI-powered file operations

## Quick Start

```bash
# Install dependencies
pip install pydantic-ai-backend[console]

# Set your API key
export ANTHROPIC_API_KEY="your-key"

# Run interactive mode
python cli_agent.py

# Or run a single task
python cli_agent.py --task "Create a hello.py that prints Hello World"
```

## Usage

```bash
# Interactive mode in current directory
python cli_agent.py

# Specify working directory
python cli_agent.py --dir /path/to/project

# Use a different model
python cli_agent.py --model anthropic:claude-sonnet-5-5

# Disable shell execution (safer)
python cli_agent.py --no-execute

# Refuse every change and command; only the read tools are offered
python cli_agent.py --read-only

# Include hidden files when searching with grep
python cli_agent.py --include-hidden

# Run single task
python cli_agent.py --task "List all Python files and count lines of code"
```

## Features

The agent can:
- **Read files** - View file contents with line numbers
- **Write files** - Create new files or overwrite existing
- **Edit files** - Make targeted changes to files
- **Search** - Find files (glob) and search contents (grep)
- **Execute** - Run shell commands (tests, builds, etc.)

## Example Session

```
$ python cli_agent.py --dir ~/myproject

CLI Agent ready! Working directory: /Users/me/myproject
Type 'quit' to exit, 'help' for examples.

You: Show me the project structure

Agent: Let me explore the project structure.

Contents of .:
  src/
  tests/
  README.md
  pyproject.toml

The project has:
- `src/` - Source code directory
- `tests/` - Test files
- `README.md` - Project documentation
- `pyproject.toml` - Python project configuration

You: Find all TODO comments

Agent: Searching for TODO comments...

Found 3 matches:
  src/main.py:45: # TODO: Add error handling
  src/utils.py:12: # TODO: Optimize this loop
  tests/test_main.py:8: # TODO: Add more test cases

You: Run the tests

Agent: Running pytest...

===== test session starts =====
collected 5 items
tests/test_main.py .....
===== 5 passed in 0.12s =====

All tests passed!
```

## Security

- `LocalWorkspace` runs commands with your user permissions and isolates nothing:
  the working directory is where commands start, not a boundary
- Use `--read-only` to refuse every change, or `--no-execute` to drop shell commands
- For code you did not write, use an isolated workspace such as `DockerWorkspace`

---

<div align="center">

### Need help implementing this in your company?

<p>We're <a href="https://vstorm.co"><b>Vstorm</b></a> — an Applied Agentic AI Engineering Consultancy<br>with 30+ production AI agent implementations.</p>

<a href="https://vstorm.co/contact-us/">
  <img src="https://img.shields.io/badge/Talk%20to%20us%20%E2%86%92-0066FF?style=for-the-badge&logoColor=white" alt="Talk to us">
</a>

<br><br>

Made with ❤️ by <a href="https://vstorm.co"><b>Vstorm</b></a>

</div>
