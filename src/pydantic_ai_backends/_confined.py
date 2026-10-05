"""A workspace whose file operations stay inside its working directory."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.workspaces import FileEntry, Workspace, WorkspaceError, WrapperWorkspace


class WorkspacePathError(WorkspaceError, PermissionError):
    """A file operation named a path outside a confined workspace."""


class ConfinedWorkspace(WrapperWorkspace):
    """Keep file operations inside the wrapped workspace's working directory.

    A path is checked where it really leads - resolved against the working
    directory, then through every symlink - so neither `..` nor a link reaches
    past the root. The console's `glob` and `grep` check their search root the
    same way.

    **Commands are not confined.** A shell reaches any file its user can, and a
    string check of a command line is not a boundary; isolate commands with a
    sandboxed workspace such as `DockerWorkspace`. This keeps the file tools -
    which typically run without approval - where `LocalWorkspace` points them,
    as `LocalBackend(root_dir=...)` did before 0.2.30.

    Example:
        ```python
        from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace

        from pydantic_ai_backends.workspaces import ConfinedWorkspace

        project = ConfinedWorkspace(Workspace(LocalWorkspaceBackend("./project")))
        result = await agent.run(prompt, workspace=project)
        ```
    """

    def __init__(self, wrapped: Workspace) -> None:
        super().__init__(wrapped)
        self._root: str | None = None

    async def _confined_root(self) -> str:
        if self._root is None:
            self._root = await self.realpath(await self.working_dir())
        return self._root

    async def contains(self, path: str) -> bool:
        """Whether `path`, relative to the working directory or absolute, leads inside it."""
        root = await self._confined_root()
        target = await self.realpath(await self.resolve(path))
        return target == root or target.startswith(root.rstrip("/") + "/")

    async def check(self, path: str) -> None:
        """Refuse `path` when it leads outside the working directory.

        Raises:
            WorkspacePathError: It does.
        """
        if not await self.contains(path):
            raise WorkspacePathError(
                f"{path!r} is outside the workspace ({await self._confined_root()})"
            )

    async def read_bytes(self, path: str) -> bytes:
        await self.check(path)
        return await super().read_bytes(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self.check(path)
        await super().write_bytes(path, data)

    async def stat(self, path: str) -> FileEntry:
        await self.check(path)
        return await super().stat(path)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        await self.check(path)
        return await super().list_dir(path)

    async def make_dir(self, path: str) -> None:
        await self.check(path)
        await super().make_dir(path)

    async def remove(self, path: str) -> None:
        await self.check(path)
        await super().remove(path)

    async def exists(self, path: str) -> bool:
        await self.check(path)
        return await super().exists(path)


def confinement(workspace: Workspace) -> ConfinedWorkspace | None:
    """The `ConfinedWorkspace` among `workspace`'s layers, if there is one."""
    layer: object = workspace
    while isinstance(layer, Workspace):
        if isinstance(layer, ConfinedWorkspace):
            return layer
        layer = layer._backend  # pyright: ignore[reportPrivateUsage]
    return None
