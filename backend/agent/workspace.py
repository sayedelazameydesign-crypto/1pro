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
the *tool interface* against a value that reaches it -- the tools that read, write,
list and delete inside a task's directory. `code_exec` does not come through here:
it runs a program that calls the kernel's own `open()` inside the R7.3 mount
namespace and never asks this module for permission. The real boundary is that
private root and the namespaces around it; this is a door on the other side of the
house, and calling it the sandbox would be the overstatement this repository keeps
paying for.

`ROOT`, therefore, is a constant and not a knob read from the deployment
environment, for the same reason `sandbox.py` reads none: a rule whose origin comes
from outside cannot be audited from inside.

Symlinks are refused, not resolved. `openat2` with `RESOLVE_BENEATH` and
`RESOLVE_NO_SYMLINKS` is the first layer where the kernel has it (5.6 or newer, and
not blocked by the container's seccomp profile); where it does not, the same rules
are walked one component at a time with `O_NOFOLLOW`, and the difference is
reported (`mechanism()`): the atomic version is a kernel guarantee, the walk has a
window between opening a component and using it, and a reader is entitled to know
which one produced their answer. What does *not* change between the two is the
policy: a symlink is refused even when it points inside the root, because telling a
safe link from a hostile one means following it, and following it is the window.
The cost is real -- a task cannot `ln -s` inside its own workspace -- and it is
stated here, in the tool's refusal code (`symlink_refused`), and in the matrix, so
nobody has to discover it by surprise.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import re
import stat

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


# --- containment: which file a tool is allowed to open ------------------------
#
# The rule the two mechanisms below implement, stated once: a tool may reach a file
# *under* the task's root and may not leave it, and it may not be led out by a link.
# `openat2` does it in one syscall; the walk does the same thing with the calls that
# have existed since the beginning, without the atomicity.

MECHANISM_OPENAT2 = "openat2"
MECHANISM_WALK = "openat-walk"

_OPENAT2 = 437              # the syscall number, the same on every architecture since 5.6
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_RESOLVE = _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_MAGICLINKS

_REFUSAL_CODES = {
    errno.ELOOP: ("symlink_refused",
                  "a symlink inside the workspace is refused, not followed"),
    errno.EXDEV: ("path_outside_root", "the path leaves the task's directory"),
    errno.ENOENT: ("no_such_file", "no such file inside the workspace"),
    errno.EISDIR: ("not_a_regular_file", "that path is a directory"),
    errno.ENOTDIR: ("not_a_directory", "a part of that path is not a directory"),
    errno.EACCES: ("permission_denied", "the workspace entry cannot be reached"),
    errno.EPERM: ("permission_denied", "the workspace entry cannot be reached"),
    errno.EEXIST: ("already_exists", "that path already exists"),
    errno.ENXIO: ("not_a_regular_file", "that fifo has no reader"),
}


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("resolve", ctypes.c_uint64)]


_MECHANISM = None


def _libc():
    """`libc` with the errno preserved across the syscall. Cached: dlopen per call
    would be a syscall's worth of work done for every file a tool touches."""
    global _LIBC
    try:
        return _LIBC
    except NameError:
        pass
    _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    return _LIBC


def _raw_openat2(dir_fd, name, flags, mode=0):
    """-> fd, or a negative errno. Numbers, not exceptions: the caller maps them."""
    how = _OpenHow(flags, mode, _RESOLVE)
    libc = _libc()
    ctypes.set_errno(0)
    result = libc.syscall(_OPENAT2, ctypes.c_int(dir_fd), name.encode("utf-8"),
                          ctypes.byref(how), ctypes.sizeof(how))
    return result if result >= 0 else -ctypes.get_errno()


def mechanism(refresh=False):
    """Which containment this kernel actually gives: asked once, with a real call.

    "The kernel refuses" and "we noticed" are different guarantees, and the
    difference is exactly the window `openat2` closes. Probing rather than assuming
    matters because the kernel is the *host's*, not the container image's: a runner
    on 5.4 has no `openat2`, and a container whose seccomp profile forbids it gets
    `EPERM` on a machine that has it.
    """
    global _MECHANISM
    if _MECHANISM is None or refresh:
        _MECHANISM = MECHANISM_WALK
        try:
            root_fd = os.open("/", os.O_PATH | os.O_CLOEXEC)
        except OSError:
            root_fd = None
        if root_fd is not None:
            try:
                if _raw_openat2(root_fd, ".", os.O_PATH | os.O_CLOEXEC) >= 0:
                    _MECHANISM = MECHANISM_OPENAT2
            finally:
                os.close(root_fd)
    return _MECHANISM


def _relative_parts(relative):
    """The components of a workspace-relative path, or a refusal.

    `..` and absolutes are refused rather than normalised, for the same reason a
    hostile id is: a repaired path is a path nobody asked for.
    """
    text = relative if isinstance(relative, str) else ""
    if not text or "\x00" in text:
        raise WorkspaceRefused("unsafe_path", "the path must name something inside the workspace")
    if os.path.isabs(text):
        raise WorkspaceRefused("unsafe_path", "the path must be relative to the workspace")
    parts = [part for part in text.split(os.sep) if part not in ("", ".")]
    if not parts or ".." in parts:
        raise WorkspaceRefused("unsafe_path", "the path must not contain `..`")
    return parts


def _root_fd(root):
    """A descriptor of the task's own directory -- the anchor every open is relative to."""
    checked = check_mount(root)
    try:
        return os.open(checked, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise _refused(error, checked)


def _workspace_refusal(number, path):
    """One errno, one code, one message -- so both mechanisms refuse identically."""
    code, message = _REFUSAL_CODES.get(number, ("io_error", os.strerror(number)))
    return WorkspaceRefused(code, f"{message}: {path}")


def _refused(error, path):
    return _workspace_refusal(error.errno, path)


def _is_symlink(dir_fd, name):
    """`lstat` through a descriptor: never a follow, so it cannot leave the root."""
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except OSError:
        return False


def _walk(root_fd, parts, flags, mode, create_parents):
    """The containment without `openat2`: one component at a time, links refused.

    Not atomic -- between opening a component and opening the next one, the entry
    can be replaced -- which is why `mechanism()` reports which of the two ran. What
    it does hold is the *policy*: no symlink is ever traversed, and no step escapes
    the descriptor it started from.
    """
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if not last:
                try:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=fd)
                except FileNotFoundError as error:
                    if not create_parents:
                        raise _refused(error, part)
                    os.mkdir(part, dir_fd=fd)
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=fd)
                except OSError as error:
                    # A symlinked *directory* component fails as ENOTDIR here (once
                    # `O_NOFOLLOW` applies, a link is not a directory) while `openat2`
                    # reports ELOOP for it. Same policy, one answer: the link is named
                    # as a link, so a caller cannot read "not a directory" and go
                    # looking for a filesystem problem that is not there.
                    if error.errno in (errno.ENOTDIR, errno.ELOOP) and _is_symlink(fd, part):
                        raise _workspace_refusal(errno.ELOOP, part)
                    raise _refused(error, part)
                os.close(fd)
                fd = next_fd
            else:
                try:
                    return os.open(part, flags | os.O_NOFOLLOW, mode, dir_fd=fd)
                except OSError as error:
                    raise _refused(error, part)
    finally:
        os.close(fd)


def _open_at(root_fd, parts, flags, mode, create_parents):
    """The one place both mechanisms are chosen between. Returns a fresh fd."""
    if mechanism() == MECHANISM_OPENAT2:
        # `how.mode` must be zero unless the flags create something -- openat2 returns
        # EINVAL rather than ignoring it, which is the sort of detail that turns a
        # refusal into a mystery if it is not handled here.
        creating = bool(flags & (os.O_CREAT | os.O_TMPFILE))
        result = _raw_openat2(root_fd, os.sep.join(parts), flags, mode if creating else 0)
        if result >= 0:
            return result
        # `openat2` cannot create a missing parent directory, so that one case is
        # handed to the walk -- and only when the caller asked for it. Everything
        # else is a refusal, and a refusal must read the same whichever mechanism
        # produced it: EXDEV from RESOLVE_BENEATH, ELOOP from a symlink, ENOTDIR,
        # EACCES. They arrive as errno values and leave as codes.
        if not (-result == errno.ENOENT and create_parents):
            raise _workspace_refusal(-result, os.sep.join(parts))
    return _walk(root_fd, parts, flags, mode, create_parents)


def open_file(root, relative, *, write=False, create=False, truncate=False,
              create_parents=False):
    """An fd for one file under the task's root, or a refusal.

    `O_NONBLOCK` is not decoration: a fifo left inside the workspace would otherwise
    block the request thread forever -- an agent that can hang the server has found
    a denial of service with no exploit needed. Regular files are unaffected by it,
    and anything that is not a regular file is refused below.
    """
    parts = _relative_parts(relative)
    root_fd = _root_fd(root)
    try:
        flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        if truncate:
            flags |= os.O_TRUNC
        fd = _open_at(root_fd, parts, flags, 0o600, create_parents)
    finally:
        os.close(root_fd)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise WorkspaceRefused(
                "not_a_regular_file",
                f"a tool may open a regular file, not this: {relative}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def list_dir(root, relative="."):
    """The entries of one directory under the root: names and their kinds.

    Returned as data rather than as a formatted listing, because a listing is copy
    and a caller (or a test) should be able to act on it without parsing a sentence.
    """
    parts = _relative_parts(relative) if relative not in (".", "") else []
    root_fd = _root_fd(root)
    try:
        if parts:
            fd = _open_at(root_fd, parts, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, 0, False)
        else:
            fd = os.dup(root_fd)
    finally:
        os.close(root_fd)
    try:
        with os.scandir(fd) as entries:
            out = []
            for entry in entries:
                kind = ("dir" if entry.is_dir(follow_symlinks=False)
                        else "symlink" if entry.is_symlink()
                        else "file" if entry.is_file(follow_symlinks=False)
                        else "other")
                out.append({"name": entry.name, "kind": kind,
                            "size": entry.stat(follow_symlinks=False).st_size})
            return sorted(out, key=lambda item: item["name"])
    finally:
        os.close(fd)
