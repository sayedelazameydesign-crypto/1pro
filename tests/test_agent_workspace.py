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


if __name__ == "__main__":
    unittest.main()
