"""Run the v2.5.1 source tree as a loopback UI server for before/after shots."""
from __future__ import annotations

import argparse
import pathlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()

    baseline = pathlib.Path(args.baseline).resolve()
    sys.path.insert(0, str(baseline))
    import gui
    import server_app
    from server_config import RuntimeConfig

    original = gui.Api.get_settings

    def get_settings(self):
        result = original(self)
        if isinstance(result, dict):
            result["mode"] = "desktop"
            result["can_open_local"] = True
            result["can_choose_folder"] = True
        return result

    gui.Api.get_settings = get_settings
    config = RuntimeConfig(
        data_dir=pathlib.Path(args.data_dir).resolve(),
        host="127.0.0.1",
        port=args.port,
        desktop_mode=False,
    )
    server_app.run_blocking(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
