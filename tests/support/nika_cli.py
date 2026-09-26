"""``nika`` CLI entry with test-only scenarios (e.g. ``simple_bgp``) registered.

Run as ``uv run python -m tests.support.nika_cli <args>``. Registration runs at
import so spawn trial workers, which re-import this module, see it too.
"""

from nika.cli.main import main
from tests.support.scenarios import register_test_scenarios

register_test_scenarios()

if __name__ == "__main__":
    main()
