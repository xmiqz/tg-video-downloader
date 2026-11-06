"""v2.5.2 NAS test package stays isolated from the production stack."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_COMPOSE = ROOT / "deploy" / "v2.5.2-test" / "compose.yaml"


def test_test_stack_uses_independent_names_port_and_volume():
    text = TEST_COMPOSE.read_text(encoding="utf-8")
    assert "name: tvg-v252-test" in text
    assert "container_name: tvg-v2-5-2-test" in text
    assert "${TGDL_TEST_PORT:-18787}:8787" in text
    assert "name: tvg-v2-5-2-test-data" in text
    assert "tg-video-downloader-data" not in text
    assert "container_name: tg-video-downloader" not in text


def test_test_image_carries_release_and_commit_short_sha():
    text = TEST_COMPOSE.read_text(encoding="utf-8")
    assert "TGDL_VERSION: v2.5.2" in text
    assert "TGDL_BUILD: 1d1b9f4" in text
    assert "image: tvg-v252-test:1d1b9f4" in text
