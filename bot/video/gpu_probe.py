"""HelloDJ — GPU probe: detect Intel QSV hardware availability at startup."""

from __future__ import annotations

import asyncio
import glob
import logging
import re

log = logging.getLogger(__name__)


class GPUUnavailableError(Exception):
    """Raised when a video command requires GPU but none is available."""

    def __init__(self) -> None:
        super().__init__(
            "Video streaming unavailable: Intel GPU device not detected"
        )


class GPUProbe:
    """Detect Intel QSV GPU availability at startup.

    Checks for /dev/dri/renderD* render nodes and runs `vainfo` to verify
    VA-API driver functionality. Results are cached after the first probe.
    """

    def __init__(self) -> None:
        self._gpu_available: bool = False
        self._render_device: str | None = None
        self._vaapi_capabilities: dict = {}

    @property
    def gpu_available(self) -> bool:
        """Whether QSV hardware acceleration is available."""
        return self._gpu_available

    @property
    def render_device(self) -> str | None:
        """Path to the render device node (e.g., /dev/dri/renderD128)."""
        return self._render_device

    @property
    def vaapi_capabilities(self) -> dict:
        """Parsed VA-API capabilities from vainfo output."""
        return self._vaapi_capabilities

    def require_gpu(self) -> None:
        """Raise GPUUnavailableError if GPU is not available.

        Use as a pre-check before video commands.
        """
        if not self._gpu_available:
            raise GPUUnavailableError()

    async def probe(self) -> None:
        """Check for render device and run vainfo to verify VA-API.

        Sets gpu_available to True only if both a render device exists AND
        vainfo executes successfully. Logs the result at INFO level.
        """
        # Step 1: Check for /dev/dri/renderD* devices
        render_devices = sorted(glob.glob("/dev/dri/renderD*"))
        if not render_devices:
            self._gpu_available = False
            self._render_device = None
            self._vaapi_capabilities = {}
            log.info("GPU probe: no render device found at /dev/dri/renderD*")
            return

        self._render_device = render_devices[0]
        log.info("GPU probe: found render device %s", self._render_device)

        # Step 2: Run vainfo to check VA-API driver
        try:
            proc = await asyncio.create_subprocess_exec(
                "vainfo",
                "--display", "drm",
                "--device", self._render_device,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except FileNotFoundError:
            self._gpu_available = False
            self._vaapi_capabilities = {}
            log.info("GPU probe: vainfo binary not found — GPU unavailable")
            return
        except asyncio.TimeoutError:
            self._gpu_available = False
            self._vaapi_capabilities = {}
            log.info("GPU probe: vainfo timed out — GPU unavailable")
            return
        except OSError as exc:
            self._gpu_available = False
            self._vaapi_capabilities = {}
            log.info("GPU probe: failed to run vainfo (%s) — GPU unavailable", exc)
            return

        if proc.returncode != 0:
            self._gpu_available = False
            self._vaapi_capabilities = {}
            stderr_text = stderr.decode(errors="replace").strip()
            log.info(
                "GPU probe: vainfo exited with code %d — GPU unavailable. stderr: %s",
                proc.returncode,
                stderr_text[:500],
            )
            return

        # Step 3: Parse vainfo output
        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        self._vaapi_capabilities = _parse_vainfo(output)
        self._gpu_available = True
        log.info(
            "GPU probe: VA-API available on %s — driver: %s, profiles: %d, entrypoints: %d",
            self._render_device,
            self._vaapi_capabilities.get("driver", "unknown"),
            len(self._vaapi_capabilities.get("profiles", [])),
            len(self._vaapi_capabilities.get("entrypoints", [])),
        )


def _parse_vainfo(output: str) -> dict:
    """Parse vainfo text output into a structured capabilities dict.

    Returns a dict with keys:
        - driver: str (VA-API driver string)
        - profiles: list[str] (supported VA profiles)
        - entrypoints: list[str] (supported entrypoints)
        - max_resolution: str | None (max resolution if reported)
    """
    caps: dict = {
        "driver": "unknown",
        "profiles": [],
        "entrypoints": [],
        "max_resolution": None,
    }

    # Extract driver string
    driver_match = re.search(r"vainfo:\s+Driver version:\s*(.+)", output)
    if driver_match:
        caps["driver"] = driver_match.group(1).strip()

    # Extract profiles
    profiles: list[str] = []
    entrypoints: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        # Lines like "VAProfileH264Main          : VAEntrypointVLD"
        profile_match = re.match(r"(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)", line)
        if profile_match:
            profile = profile_match.group(1)
            entrypoint = profile_match.group(2)
            if profile not in profiles:
                profiles.append(profile)
            if entrypoint not in entrypoints:
                entrypoints.append(entrypoint)

    caps["profiles"] = profiles
    caps["entrypoints"] = entrypoints

    # Extract max resolution if present
    res_match = re.search(r"max.*?resolution[:\s]+(\d+)\s*x\s*(\d+)", output, re.IGNORECASE)
    if res_match:
        caps["max_resolution"] = f"{res_match.group(1)}x{res_match.group(2)}"

    return caps
