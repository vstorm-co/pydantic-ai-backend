# Web Production Example

Multi-user web service with isolated Docker sandboxes, AI agent, and web UI.

## Features

- **Web UI** - Chat interface at http://localhost:8000
- **User Isolation** - Each user gets their own Docker container
- **AI Agent** - Chat with AI that can create, edit, run code
- **File Browser** - View files created by the AI

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Web UI (templates/)                      │
├─────────────────────────────────────────────────────────────┤
│                      FastAPI Server                         │
├─────────────────────────────────────────────────────────────┤
│        DockerWorkspace — one container per session ref      │
├───────────────┬───────────────┬───────────────┬────────────┤
│   container   │   container   │   container   │    ...     │
│  (session A)  │  (session B)  │  (session C)  │            │
└───────────────┴───────────────┴───────────────┴────────────┘
```

## Quick Start

```bash
# Install dependencies
pip install "pydantic-ai-backend[console,docker]" fastapi uvicorn jinja2

# Set API key
export ANTHROPIC_API_KEY="your-key"

# Start server
python server.py
```

Open http://localhost:8000 in your browser.

## Screenshots

The UI provides:
- Chat panel (left) - Talk to the AI assistant
- File browser (right) - See files in your sandbox

Try asking:
- "Create a hello.py that prints Hello World"
- "Run hello.py"
- "Create a fibonacci function and test it"

## API Endpoints

The server also exposes a REST API:

```bash
# Create session
curl -X POST "http://localhost:8000/sessions"

# Chat with AI
curl -X POST "http://localhost:8000/sessions/{id}/chat" \
  -H "Content-Type: application/json" \
  -d '{"message": "Create a script"}'

# List files
curl "http://localhost:8000/sessions/{id}/files"

# Read file
curl "http://localhost:8000/sessions/{id}/files/hello.py"

# Execute command
curl -X POST "http://localhost:8000/sessions/{id}/execute" \
  -H "Content-Type: application/json" \
  -d '{"command": "python hello.py"}'
```

## Project Structure

```
web_production/
├── server.py           # FastAPI server with AI agent
├── templates/
│   └── index.html      # Web UI
└── README.md
```

## Security Notes

- Each browser session gets isolated Docker container
- Users cannot access other users' files
- Containers auto-cleanup on session end

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
