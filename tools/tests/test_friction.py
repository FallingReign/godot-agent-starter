from __future__ import annotations

import unittest
from unittest import mock

from tools import friction


class CurrentFileChurnTests(unittest.TestCase):
    def test_deleted_project_files_do_not_appear_in_churn(self) -> None:
        commits = []
        for index in range(4):
            commits.extend(
                (
                    f"{index:040x}",
                    "src/project.godot",
                    "src/content/maps/removed_fixture.json",
                )
            )

        with mock.patch.object(
            friction, "git", return_value=(0, "\n".join(commits))
        ):
            counts = friction.churn("")

        self.assertEqual(counts["project.godot"], 4)
        self.assertNotIn("content/maps/removed_fixture.json", counts)


if __name__ == "__main__":
    unittest.main()
