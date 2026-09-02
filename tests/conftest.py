"""Put the flat Agent/ directory on sys.path so tests import its modules the same
way the Lambda runtime does (agent.py, aws_athena_cur.py, ... at the top level)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Agent"))
