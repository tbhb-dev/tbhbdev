# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Check the Grafana dashboard against the launch trace contract."""

import json
import unittest
from pathlib import Path


class DashboardTest(unittest.TestCase):
    def test_launch_queries_use_tempo(self):
        path = Path(__file__).parents[2] / "dashboards/agents-live.json"
        dashboard = json.loads(path.read_text())
        targets = [
            target for panel in dashboard["panels"] for target in panel["targets"]
        ]
        self.assertTrue(targets)
        for target in targets:
            self.assertEqual(target["datasource"]["uid"], "tempo")
            self.assertIn("tbhbdev alpha agent exec", target["query"])


if __name__ == "__main__":
    unittest.main()
