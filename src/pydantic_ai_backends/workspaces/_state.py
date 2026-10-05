"""A `StateBackend` document as a Pydantic AI workspace."""

from __future__ import annotations

import posixpath
import uuid
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import (
    FileEntry,
    SupportsFilesystem,
    WorkspaceBackend,
    WorkspaceRef,
    WorkspaceUnavailableError,
)

from pydantic_ai_backends.backends.state import StateBackend

STATE_PROVIDER = "state"
"""`WorkspaceRef.provider` of a document workspace."""

STATE_ROOT = "/"
"""The working directory of every document workspace."""


class StateWorkspaceBackend(WorkspaceBackend, SupportsFilesystem):
    """A `StateBackend` document, as the environment an agent run works in.

    Files only: there is nothing here to run a command in, so `ctx.workspace.run`
    refuses and file tools work. The document lives in `store` under the ref's
    id; a run with no ref adds a new one on its first operation.

    Args:
        store: Documents by id. The application owns it — an in-process dict, or
            a mapping it filled from its own storage before the run.
        ref: The document to work in; `None` creates one on first use.
    """

    def __init__(
        self, store: MutableMapping[str, StateBackend], *, ref: WorkspaceRef | None = None
    ) -> None:
        if ref is not None and ref.provider != STATE_PROVIDER:
            raise ValueError(f"expected a {STATE_PROVIDER!r} workspace ref, got {ref.provider!r}")
        self._store = store
        self._ref = ref

    @property
    def ref(self) -> WorkspaceRef | None:
        """The document's id once it exists; `None` before the first operation."""
        return self._ref

    def _state(self) -> StateBackend:
        """The document, created on the first operation when there is no ref yet."""
        if self._ref is None:
            document_id = uuid.uuid4().hex
            self._store[document_id] = StateBackend()
            self._ref = WorkspaceRef(provider=STATE_PROVIDER, id=document_id)
        state = self._store.get(self._ref.id)
        if state is None:
            raise WorkspaceUnavailableError(f"state document {self._ref.id!r} no longer exists")
        return state

    async def working_dir(self) -> str:
        """The document's root, which relative paths resolve against."""
        self._state()
        return STATE_ROOT

    async def read_bytes(self, path: str) -> bytes:
        """A file's exact bytes."""
        return self._state().read_bytes(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Store a file, creating missing parents."""
        self._state().write_bytes(path, data)

    async def stat(self, path: str) -> FileEntry:
        """Metadata for a file or directory."""
        state = self._state()
        normal = posixpath.normpath(path)
        if state.is_dir(normal):
            return FileEntry(name=posixpath.basename(normal), path=normal, is_dir=True, size=None)
        return FileEntry(
            name=posixpath.basename(normal), path=normal, is_dir=False, size=state.size(normal)
        )

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        """The entries directly in a directory."""
        state = self._state()
        normal = posixpath.normpath(path)
        entries: list[FileEntry] = []
        for name, is_dir in state.list_dir(normal):
            child = posixpath.join(normal, name)
            size = None if is_dir else state.size(child)
            entries.append(FileEntry(name=name, path=child, is_dir=is_dir, size=size))
        return entries

    async def make_dir(self, path: str) -> None:
        """Create a directory and any missing parents."""
        self._state().make_dir(path)

    async def remove(self, path: str) -> None:
        """Remove a file, or a directory and everything under it.

        Raises:
            ValueError: `path` is the root, which holds the whole workspace.
        """
        state = self._state()
        if posixpath.normpath(path) == STATE_ROOT:
            raise ValueError("refusing to remove the workspace's working directory")
        state.remove(path)

    async def exists(self, path: str) -> bool:
        """Whether a file or directory is at `path`."""
        return self._state().exists(path)


@dataclass(kw_only=True)
class StateWorkspace(AbstractCapability[object]):
    """Supply a `StateBackend` document as the run's workspace.

    A run with no ref adds a document to `store`; one carrying a `"state"` ref
    works in the document under that id, and one whose document is gone fails
    with `WorkspaceUnavailableError`. Commands are not available: compose it
    with file tools such as the harness's `FileSystem`, or
    `ConsoleCapability(include_execute=False)`.

    The default `store` lives as long as this capability. An application that
    keeps documents elsewhere fills the mapping before a run and saves the
    document afterwards — `files` and `sorted(directories)` are JSON.

    Example:
        ```python
        from pydantic_ai import Agent
        from pydantic_ai_harness.filesystem import FileSystem

        from pydantic_ai_backends.workspaces import StateWorkspace

        agent = Agent("anthropic:claude-opus-5-5", capabilities=[StateWorkspace(), FileSystem()])
        ```
    """

    store: MutableMapping[str, StateBackend] = field(default_factory=dict, repr=False)
    """Documents by id; the default is an in-process dict."""

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError(
                "`StateWorkspace` does not support `defer_loading=True`: "
                "the workspace is selected before deferred capabilities load."
            )

    def backend(self, ref: WorkspaceRef | None = None) -> StateWorkspaceBackend:
        """A backend for `ref`, or for a new document created on first use."""
        return StateWorkspaceBackend(self.store, ref=ref)

    def get_workspace(
        self, ctx: RunContext[object], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        """This run's backend, or `None` for a ref another provider owns."""
        del ctx
        if ref is not None and ref.provider != STATE_PROVIDER:
            return None
        return self.backend(ref)

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Drop the document `ref` names. Already gone is fine.

        Raises:
            ValueError: `ref` belongs to another provider.
        """
        if ref.provider != STATE_PROVIDER:
            raise ValueError(f"expected a {STATE_PROVIDER!r} workspace ref, got {ref.provider!r}")
        self.store.pop(ref.id, None)
