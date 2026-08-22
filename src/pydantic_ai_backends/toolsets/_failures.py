"""Which failures steer the model, and which are simply its answer.

A tool has two ways to report that something went wrong, and they say different
things. `ModelRetry` means *you can fix this by calling again differently* — the
message goes back as a retry prompt and the model gets another attempt. A
returned string means *this is the result; reason about it* — the run moves on.

Every failure in this toolset used to take the second form, including the ones
the model could plainly have fixed: an `old_string` that matched three places, a
file edited between the read and the edit, a path that does not exist. The model
was told, in a sentence indistinguishable from a real answer, and it was left to
notice.

So the rule, and it is the same rule in every tool here:

- **Argument-fixable → `ModelRetry`.** A missing file, an offset past the end, an
  `old_string` that is absent or not unique, a stale read, a hash that no longer
  matches. Calling again with different arguments is a plausible fix.
- **Everything else → a string.** A non-zero exit from `execute` is a *result*,
  not a malformed call; `grep` finding nothing is an answer; a backend with no
  shell is a fact about the deployment; and a **permission refusal must never be
  a retry**, because a retry prompt invites the model to look for a way around
  it. Those come back as text and the model adapts.

`last_attempt` is what makes the first half safe. `ModelRetry` is not free: when
a tool exhausts its retries, pydantic-ai raises `UnexpectedModelBehavior` and the
whole run ends — so a model that mistypes an `old_string` twice would kill a run
that previously just carried on. On the final attempt the message is therefore
returned rather than raised: the model is steered while there is budget for it,
and the worst case is the behaviour this library had before, never a dead run.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import ModelRetry
from pydantic_ai.tools import RunContext

from pydantic_ai_backends.backends._guard import PERMISSION_DENIED_PREFIX


def is_refusal(message: str) -> bool:
    """Whether `message` is a permission refusal rather than a fixable mistake.

    The prefix is `PermissionGuard`'s own, imported rather than repeated: both
    ends are in this repository, and a refusal that stopped being recognised
    would quietly become a retry prompt telling the model to try again.
    """
    return PERMISSION_DENIED_PREFIX in message


def steer(ctx: RunContext[Any], message: str) -> str:
    """Ask the model to try again, while it still has an attempt left.

    Args:
        ctx: The tool call's context, for how many retries remain.
        message: What went wrong, and what a different call should do about it.

    Returns:
        `message`, on the last attempt, for the caller to return as its result.

    Raises:
        ModelRetry: Otherwise — the message reaches the model as a retry prompt.
    """
    if is_refusal(message) or ctx.last_attempt:
        return message
    raise ModelRetry(message)
