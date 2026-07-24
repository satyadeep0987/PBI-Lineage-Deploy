"""Regression tests for excluded Power BI workspaces."""

from __future__ import annotations

import unittest

from pbi_modules.app_shell import filter_excluded_workspaces


class WorkspaceInventoryFilterTests(unittest.TestCase):
    def test_admin_monitoring_is_removed_case_insensitively(self):
        workspaces = [
            {"id": "excluded-1", "name": "Admin monitoring"},
            {"id": "included-1", "name": "Sales&Marketing"},
            {"id": "excluded-2", "name": "  ADMIN MONITORING  "},
        ]

        filtered = filter_excluded_workspaces(workspaces)

        self.assertEqual(filtered, [{"id": "included-1", "name": "Sales&Marketing"}])


if __name__ == "__main__":
    unittest.main()
