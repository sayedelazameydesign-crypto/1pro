"""A task id is not a path until this file says it is.

`workspace.py` turns an id into a directory, so it is the one place a hostile
string can become a filename. These tests hold the two halves of that: an id of the
wrong shape is refused *before* anything is joined, and a workspace handed to the
runner is checked as a path (absolute, real, inside the task tree) rather than
trusted because a caller built it out of the right function.

The shapes below are the ones that actually get tried: a traversal (`../`), an
absolute path, a name that is legal on one filesystem and means something else on
another, an id that is *almost* a UUID, and the sentence that would be a brilliant
directory name if nobody were looking.
"""

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent import workspace  # noqa: E402


class AnIdIsAnIdentity(unittest.TestCase):
    def test_the_shapes_the_store_actually_writes_are_accepted(self):
        import uuid
        for value in (str(uuid.uuid4()), "0123456789abcdef", "a" * 8, "z9" * 32):
            with self.subTest(value=value[:24]):
                self.assertEqual(workspace.validate_id(value), value)

    def test_a_valid_id_is_returned_unchanged(self):
        # Not trimmed, not lowercased, not quoted: a validator that repairs is a
        # validator that hides which caller sent the wrong thing.
        self.assertEqual(workspace.validate_id("abc12345"), "abc12345")
        with self.assertRaises(workspace.WorkspaceRefused):
            workspace.validate_id(" Abc12345 ")

    def test_nothing_that_can_name_another_directory_passes(self):
        hostile = [
            "../etc", "../../etc/passwd", "/etc/passwd", "tasks/../../etc",
            "0123456789abcdef/../../root", "..", ".", "./", "0123456789abcdef/",
            "0123456789abcdef\x00", "0123456789abcdef\n", "0123456789abcde f",
            "0123456789ABCDEF",                      # uppercase is not the store's shape
            "a" * 65,                                # over the length ceiling
            "", "   ", None, 42, True, [], {"id": "0123456789abcdef"},
        ]
        for value in hostile:
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(workspace.WorkspaceRefused) as caught:
                    workspace.validate_id(value)
                self.assertEqual(caught.exception.code, "unsafe_workspace_id")

    def test_a_uuid_with_the_wrong_grouping_is_not_a_uuid(self):
        # 8-4-4-4-12 exactly, lowercase, or it is not this store's identity. The first
        # case is a *valid* UUID -- it is here to keep the others honest, because a
        # test that refuses everything is not a test of a validator.
        self.assertEqual(workspace.validate_id("01234567-89ab-cdef-0123-456789abcdef"),
                         "01234567-89ab-cdef-0123-456789abcdef")
        for value in ("012345678-9ab-cdef-0123-456789abcdef",
                      "01234567-89ab-cdef-0123-456789abcde",
                      "01234567-89ab-cdef-0123-456789abcdef0",
                      "01234567-89AB-CDEF-0123-456789ABCDEF"):
            with self.subTest(value=value):
                with self.assertRaises(workspace.WorkspaceRefused):
                    workspace.validate_id(value)

    def test_the_tree_is_built_from_the_root_in_this_file(self):
        path = workspace.task_root("0123456789abcdef", root="/tmp/tree")
        self.assertEqual(path, "/tmp/tree/tasks/0123456789abcdef")
        # The origin is the module's, and it is not reachable by naming it in an id:
        # a caller that wants a different tree passes `root`, which no model touches.
        self.assertEqual(workspace.ROOT, "/agent-workspace")
        refused = pathlib.Path(workspace.ROOT) / "tasks" / ".."
        with self.assertRaises(workspace.WorkspaceRefused):
            workspace.task_root(str(refused), root="/tmp/tree")

    def test_creating_a_root_creates_only_the_one_it_was_asked_for(self):
        with tempfile.TemporaryDirectory() as base:
            created = workspace.task_root("0123456789abcdef", root=base, create=True)
            self.assertTrue(os.path.isdir(created))
            self.assertEqual(sorted(os.listdir(base)), ["tasks"])
            self.assertEqual(sorted(os.listdir(created)), [])


class AWorkspacePathIsCheckedNotTrusted(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.patcher = mock.patch.object(workspace, "ROOT", self.base.name)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.task = workspace.task_root("0123456789abcdef", create=True)

    def test_a_real_task_directory_passes(self):
        self.assertEqual(workspace.check_mount(self.task), self.task)

    def test_a_path_outside_the_task_tree_is_refused_however_it_is_spelled(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(lambda: pathlib.Path(outside).rmdir())
        for value in (outside, os.path.join(self.base.name, "tasks", "..", "..", "etc"),
                      "tasks/0123456789abcdef", "", None):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(workspace.WorkspaceRefused) as caught:
                    workspace.check_mount(value)
                self.assertEqual(caught.exception.code, "unsafe_workspace")

    def test_a_symlink_is_refused_rather_than_followed(self):
        link = os.path.join(self.base.name, "tasks", "link")
        os.symlink(self.task, link)
        with self.assertRaises(workspace.WorkspaceRefused):
            workspace.check_mount(link)
        # Even when its target is legitimate: the point is not where it points today,
        # it is that the answer can change between the check and the open.
        self.assertTrue(os.path.isdir(link))

    def test_a_file_is_not_a_workspace(self):
        path = os.path.join(self.task, "note.txt")
        pathlib.Path(path).write_text("x", encoding="utf-8")
        with self.assertRaises(workspace.WorkspaceRefused):
            workspace.check_mount(path)

    def test_a_symlinked_tree_is_caught_by_the_real_path_comparison(self):
        # The leaf is a real directory, the spelling has no `..`, and the path is
        # absolute -- so every structural check passes, and only the resolved path
        # gives it away. This is the case the realpath comparison exists for.
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        # A directory that is genuinely elsewhere: it fails for being outside the tree
        # (asserted in the test above), which is *not* the case this test is about.
        workspace.task_root("0123456789abcdef", root=other.name, create=True)
        shadow = os.path.join(self.base.name, "tasks", "shadow")
        os.symlink(other.name, shadow)
        spelled = os.path.join(shadow, "tasks", "0123456789abcdef")
        self.assertTrue(os.path.isdir(spelled), "the spelling must look like a directory")
        self.assertNotIn("..", spelled.split(os.sep))
        with self.assertRaises(workspace.WorkspaceRefused) as caught:
            workspace.check_mount(spelled)
        self.assertEqual(caught.exception.code, "unsafe_workspace")


class ContainmentIsTheSameEitherWay(unittest.TestCase):
    """`openat2` where the kernel has it, a component-by-component walk where it does
    not -- and the *same* answers either way.

    That equality is the whole reason a fallback is allowed to exist: two mechanisms
    with two policies would be two doors, and the weaker one would be the one that
    mattered. So every case below is run under both, and the outcomes are compared
    as data (a bytes payload, or a refusal code), never as prose.
    """

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        self.patcher = mock.patch.object(workspace, "ROOT", self.base.name)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.task = workspace.task_root("0123456789abcdef", create=True)
        (pathlib.Path(self.task) / "note.txt").write_text("inside", encoding="utf-8")
        os.makedirs(os.path.join(self.task, "sub"), exist_ok=True)
        (pathlib.Path(self.task, "sub") / "deep.txt").write_text("deep", encoding="utf-8")
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        (pathlib.Path(other.name) / "secret.txt").write_text("outside", encoding="utf-8")
        os.symlink(os.path.join(other.name, "secret.txt"), os.path.join(self.task, "escape"))
        os.symlink("note.txt", os.path.join(self.task, "inside-link"))
        os.symlink(other.name, os.path.join(self.task, "way-out"))
        os.mkfifo(os.path.join(self.task, "pipe"))

    def outcome(self, relative, **kwargs):
        """`("ok", payload)` or `("refused", code)`: the shape a test can compare."""
        try:
            fd = workspace.open_file(self.task, relative, **kwargs)
        except workspace.WorkspaceRefused as refused:
            return ("refused", refused.code)
        try:
            return ("ok", os.read(fd, 64))
        finally:
            os.close(fd)

    CASES = [
        ("note.txt", ("ok", b"inside")),
        (os.path.join("sub", "deep.txt"), ("ok", b"deep")),
        ("missing.txt", ("refused", "no_such_file")),
        ("../outside.txt", ("refused", "unsafe_path")),
        ("sub/../../outside.txt", ("refused", "unsafe_path")),
        ("/etc/passwd", ("refused", "unsafe_path")),
        ("", ("refused", "unsafe_path")),
        ("escape", ("refused", "symlink_refused")),                 # a link out
        ("inside-link", ("refused", "symlink_refused")),            # a link in
        ("way-out/secret.txt", ("refused", "symlink_refused")),     # a linked directory
        ("pipe", ("refused", "not_a_regular_file")),                # a fifo, not a wait
    ]

    def test_the_policy_holds_under_whatever_mechanism_this_kernel_gives(self):
        for relative, expected in self.CASES:
            with self.subTest(case=relative):
                self.assertEqual(self.outcome(relative), expected)

    def test_the_two_mechanisms_refuse_identically(self):
        available = [workspace.MECHANISM_WALK]
        if workspace.mechanism() == workspace.MECHANISM_OPENAT2:
            available.insert(0, workspace.MECHANISM_OPENAT2)
        seen = {}
        for name in available:
            with mock.patch.object(workspace, "_MECHANISM", name):
                self.assertEqual(workspace.mechanism(), name)
                seen[name] = [self.outcome(relative) for relative, _ in self.CASES]
                for (relative, _), result in zip(self.CASES, seen[name]):
                    self.assertEqual(result, dict(self.CASES)[relative],
                                     f"{name} answered differently for {relative}")
        if len(seen) > 1:
            self.assertEqual(seen[workspace.MECHANISM_OPENAT2], seen[workspace.MECHANISM_WALK],
                             "the fallback is a second policy, not a second mechanism")

    def test_a_fifo_is_refused_instead_of_hanging_the_request(self):
        """An agent that can block a worker thread forever is a denial of service
        with no exploit required, so the open must not wait for a reader."""
        started = time.time()
        self.assertEqual(self.outcome("pipe"), ("refused", "not_a_regular_file"))
        self.assertLess(time.time() - started, 1.0)

    def test_writing_creates_inside_the_root_and_nowhere_near_the_outside(self):
        for forced in (workspace.MECHANISM_WALK,
                       workspace.MECHANISM_OPENAT2 if
                       workspace.mechanism() == workspace.MECHANISM_OPENAT2 else None):
            if forced is None:
                continue
            with self.subTest(mechanism=forced), mock.patch.object(workspace, "_MECHANISM", forced):
                fd = workspace.open_file(self.task, "a/b/new.txt", write=True, create=True,
                                         create_parents=True)
                os.write(fd, b"written")
                os.close(fd)
                self.assertEqual((pathlib.Path(self.task) / "a/b/new.txt").read_text(), "written")
                with self.assertRaises(workspace.WorkspaceRefused) as caught:
                    workspace.open_file(self.task, "escape/../escape", write=True, create=True)
                self.assertEqual(caught.exception.code, "unsafe_path")

    def test_a_listing_is_data_and_a_symlink_is_labelled_not_followed(self):
        listing = {entry["name"]: entry["kind"] for entry in workspace.list_dir(self.task)}
        self.assertEqual(listing["note.txt"], "file")
        self.assertEqual(listing["sub"], "dir")
        self.assertEqual(listing["escape"], "symlink")
        self.assertEqual(listing["pipe"], "other")
        self.assertEqual(sorted(listing), ["escape", "inside-link", "note.txt", "pipe", "sub", "way-out"])

    def test_it_says_which_containment_is_in_force(self):
        self.assertIn(workspace.mechanism(),
                      (workspace.MECHANISM_OPENAT2, workspace.MECHANISM_WALK))
        # Forcing the probe to re-run must give the same answer: a mechanism that
        # changes between two calls would mean the reporting means nothing.
        self.assertEqual(workspace.mechanism(refresh=True), workspace.mechanism())


if __name__ == "__main__":
    unittest.main()
