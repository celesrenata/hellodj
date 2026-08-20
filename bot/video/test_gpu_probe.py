"""Tests for GPUProbe — verifies GPU detection logic and error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from video.gpu_probe import GPUProbe, GPUUnavailableError, _parse_vainfo


# ── Unit tests for _parse_vainfo ──────────────────────────────────────

SAMPLE_VAINFO_OUTPUT = """\
vainfo: VA-API version: 1.20 (libva 2.20.0)
vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 23.4.0
vainfo: Supported profile and target pairs:
      VAProfileH264Main               : VAEntrypointVLD
      VAProfileH264Main               : VAEntrypointEncSlice
      VAProfileH264High               : VAEntrypointVLD
      VAProfileH264High               : VAEntrypointEncSlice
      VAProfileHEVCMain               : VAEntrypointVLD
      VAProfileHEVCMain               : VAEntrypointEncSlice
      VAProfileHEVCMain10             : VAEntrypointVLD
      VAProfileVP9Profile0            : VAEntrypointVLD
"""


def test_parse_vainfo_extracts_driver():
    caps = _parse_vainfo(SAMPLE_VAINFO_OUTPUT)
    assert "Intel iHD" in caps["driver"]


def test_parse_vainfo_extracts_profiles():
    caps = _parse_vainfo(SAMPLE_VAINFO_OUTPUT)
    assert "VAProfileH264Main" in caps["profiles"]
    assert "VAProfileH264High" in caps["profiles"]
    assert "VAProfileHEVCMain" in caps["profiles"]
    assert "VAProfileVP9Profile0" in caps["profiles"]


def test_parse_vainfo_extracts_entrypoints():
    caps = _parse_vainfo(SAMPLE_VAINFO_OUTPUT)
    assert "VAEntrypointVLD" in caps["entrypoints"]
    assert "VAEntrypointEncSlice" in caps["entrypoints"]


def test_parse_vainfo_no_duplicates():
    caps = _parse_vainfo(SAMPLE_VAINFO_OUTPUT)
    assert len(caps["profiles"]) == len(set(caps["profiles"]))
    assert len(caps["entrypoints"]) == len(set(caps["entrypoints"]))


def test_parse_vainfo_empty_output():
    caps = _parse_vainfo("")
    assert caps["driver"] == "unknown"
    assert caps["profiles"] == []
    assert caps["entrypoints"] == []
    assert caps["max_resolution"] is None


def test_parse_vainfo_max_resolution():
    output = SAMPLE_VAINFO_OUTPUT + "\n      max resolution: 4096x4096\n"
    caps = _parse_vainfo(output)
    assert caps["max_resolution"] == "4096x4096"


# ── Unit tests for GPUProbe ───────────────────────────────────────────

@pytest.fixture
def probe():
    return GPUProbe()


def test_initial_state(probe):
    """GPUProbe starts with gpu_available=False."""
    assert probe.gpu_available is False
    assert probe.render_device is None
    assert probe.vaapi_capabilities == {}


def test_require_gpu_raises_when_unavailable(probe):
    """require_gpu() raises GPUUnavailableError when gpu_available is False."""
    with pytest.raises(GPUUnavailableError) as exc_info:
        probe.require_gpu()
    assert "Intel GPU device not detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_probe_no_render_device(probe):
    """probe() sets gpu_available=False when no /dev/dri/renderD* exists."""
    with patch("video.gpu_probe.glob.glob", return_value=[]):
        await probe.probe()
    assert probe.gpu_available is False
    assert probe.render_device is None


@pytest.mark.asyncio
async def test_probe_vainfo_not_found(probe):
    """probe() sets gpu_available=False when vainfo binary is missing."""
    with patch("video.gpu_probe.glob.glob", return_value=["/dev/dri/renderD128"]):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("vainfo")):
            await probe.probe()
    assert probe.gpu_available is False
    assert probe.render_device == "/dev/dri/renderD128"


@pytest.mark.asyncio
async def test_probe_vainfo_timeout(probe):
    """probe() sets gpu_available=False when vainfo times out."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

    with patch("video.gpu_probe.glob.glob", return_value=["/dev/dri/renderD128"]):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
                await probe.probe()
    assert probe.gpu_available is False


@pytest.mark.asyncio
async def test_probe_vainfo_nonzero_exit(probe):
    """probe() sets gpu_available=False when vainfo exits with non-zero code."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"error: no driver"))

    async def fake_wait_for(coro, timeout):
        return await coro

    with patch("video.gpu_probe.glob.glob", return_value=["/dev/dri/renderD128"]):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                await probe.probe()
    assert probe.gpu_available is False


@pytest.mark.asyncio
async def test_probe_success(probe):
    """probe() sets gpu_available=True when render device + vainfo succeed."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(SAMPLE_VAINFO_OUTPUT.encode(), b"")
    )

    async def fake_wait_for(coro, timeout):
        return await coro

    with patch("video.gpu_probe.glob.glob", return_value=["/dev/dri/renderD128"]):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                await probe.probe()

    assert probe.gpu_available is True
    assert probe.render_device == "/dev/dri/renderD128"
    assert "VAProfileH264Main" in probe.vaapi_capabilities["profiles"]


@pytest.mark.asyncio
async def test_probe_success_require_gpu_passes(probe):
    """require_gpu() does not raise after a successful probe."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(SAMPLE_VAINFO_OUTPUT.encode(), b"")
    )

    async def fake_wait_for(coro, timeout):
        return await coro

    with patch("video.gpu_probe.glob.glob", return_value=["/dev/dri/renderD128"]):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                await probe.probe()

    # Should not raise
    probe.require_gpu()


@pytest.mark.asyncio
async def test_probe_picks_first_sorted_device(probe):
    """probe() picks the first device when multiple render nodes exist."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(SAMPLE_VAINFO_OUTPUT.encode(), b"")
    )

    async def fake_wait_for(coro, timeout):
        return await coro

    devices = ["/dev/dri/renderD128", "/dev/dri/renderD129"]
    with patch("video.gpu_probe.glob.glob", return_value=devices):
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                await probe.probe()

    assert probe.render_device == "/dev/dri/renderD128"
