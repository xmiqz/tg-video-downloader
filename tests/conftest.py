import asyncio
import pathlib
import sys

import pytest

import tg_video_dl as core

if sys.platform == "win32":
    # The managed test sandbox blocks proactor socketpair creation.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def restore_core_data_dir():
    original = pathlib.Path(core.DATA_DIR)
    yield
    core.configure_data_dir(original)
