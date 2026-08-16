"""Global safety boundary for every pytest collection in the Ava fork."""

import os

# This root conftest is loaded before child conftests and test modules. Tests may
# exercise perception components explicitly, but collection must never start the
# background observer against a real Control Plane.
os.environ["AVA_PERCEPTION"] = "0"
