"""Where one task's files live -- and the only place an id becomes a path.

A task id is an identity, not a path. It arrives from the store as a UUID, but the
moment a string is joined onto a directory name it becomes something a caller can
aim: `../..`, an absolute path, a NUL byte, a symlink that already exists when the
check runs. So this module owns the two things a path is built from:

* the **origin** -- `ROOT` -- is written here and is not a parameter. A caller names
  a task, never a directory, so no caller can hand over a path that merely looks
  innocent;
* the **shape** of an id is validated before any join (`validate_id`: a lowercase
  UUID, or 8-64 characters of `[a-z0-9]`), and an id that fails is refused rather
  than normalised. `tasks/../../etc` is not a task, and repairing it into one would
  be the bug wearing a fix's clothes.

None of this is the security boundary, and the difference matters. This file guards
the *tool interface* against a value that reaches it -- the tools that will read,
write, list and delete inside a task's directory. `code_exec` does not come through
here: it runs a program with the kernel's own `open()` inside the R7.3 mount
namespace and never asks this module for permission. The boundary is the private
root and the namespaces around it; this is the door on the other side of the house.

`ROOT`, therefore, is a constant and not a knob read from the deployment
environment, for the same reason `sandbox.py` reads none: a rule whose origin comes
from outside cannot be audited from inside.
"""

from __future__ import annotations

import os
import re

# The origin of every task directory. `/agent-workspace` is where the deploy
# bind-mounts the task tree; tests point it elsewhere by patching this attribute,
# which is why `task_root()` takes an explicit `root` argument too.
ROOT = "/agent-workspace"

# Two accepted shapes and nothing else: a lowercase UUID (what `store.new_task`
# writes) or 8-64 characters of `[a-z0-9]`. No hyphen, slash, dot, colon or space
# can appear in a validated id, so a validated id cannot name a directory above the
# one it is joined to.
_ID = re.compile(
    r"\A(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[a-z0-9]{8,64})\Z")


class WorkspaceRefused(RuntimeError):
    """A path or an id was refused before anything was built from it."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_id(value):
    """The id, or a refusal. Never a normalisation, never a default."""
    text = value if isinstance(value, str) else ""
    if not _ID.match(text):
        raise WorkspaceRefused(
            "unsafe_workspace_id",
            "an id must be a lowercase UUID or 8-64 characters of [a-z0-9]; "
            "a path fragment is not an identity")
    return text


def task_root(task_id, root=None, create=False):
    """The directory for one task, built from an id that has already passed.

    `root` exists for tests and for an operator who moved the tree; it is never
    taken from the model, from a request, or from the id itself.
    """
    base = ROOT if root is None else root
    path = os.path.join(base, "tasks", validate_id(task_id))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def check_mount(path):
    """A workspace handed to the runner: a real directory inside the task tree.

    Structural on purpose: `..` is refused rather than collapsed, a symlink is
    refused rather than followed, and the containment test is on resolved real
    paths -- so a directory that *is* outside the tree cannot be reached by a
    spelling that looks like it is inside.
    """
    text = path if isinstance(path, str) else ""
    if not text or not os.path.isabs(text):
        raise WorkspaceRefused("unsafe_workspace", "the workspace path must be absolute")
    if ".." in text.split(os.sep):
        raise WorkspaceRefused("unsafe_workspace", "the workspace path must not contain `..`")
    if os.path.islink(text):
        raise WorkspaceRefused("unsafe_workspace", "the workspace must be a directory, not a symlink")
    if not os.path.isdir(text):
        raise WorkspaceRefused("unsafe_workspace", f"not a directory: {text}")
    inside = os.path.realpath(text).startswith(os.path.realpath(ROOT) + os.sep)
    if not inside:
        raise WorkspaceRefused("unsafe_workspace",
                               f"{os.path.realpath(text)} is outside the task tree")
    return text
