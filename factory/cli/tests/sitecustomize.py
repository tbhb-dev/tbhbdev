# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Start coverage in Python subprocesses using Bazel's wheel runfiles."""

import os

# The wheel is on PYTHONPATH, so Python does not process its startup .pth.
# The subprocess patch supplies the same configuration as the parent.
if "COVERAGE_PROCESS_CONFIG" in os.environ:
    from coverage import process_startup

    process_startup()
