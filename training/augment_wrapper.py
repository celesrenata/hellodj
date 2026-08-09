#!/usr/bin/env python3
"""Wrapper: auto-detects Linux distro / CUDA, patches known issues, pipes output through filter, runs openWakeWord train.py.

Usage:
    python augment_wrapper.py --training_config /path/to/my_custom_model.yml --generate_clips
    python augment_wrapper.py --training_config /path/to/my_custom_model.yml --augment_clips
    python augment_wrapper.py --training_config /path/to/my_custom_model.yml --train_model
"""
import sys
import os
import argparse

# ---------------------------------------------------------------------------
# Auto-detect Linux distro / CUDA library paths before any imports
# ---------------------------------------------------------------------------
def _probe_cuda_libs():
    """Return (cuda_compat_dir, cuda_home) or (None, None).

    NVIDIA NGC containers put a compat library in:
        /usr/local/cuda-<VER>/compat/
    We detect it so the wrapper can inject LD_LIBRARY_PATH.
    """
    cuda_home = os.environ.get('CUDA_HOME')
    if cuda_home and os.path.isdir(os.path.join(cuda_home, 'lib', 'compat')):
        _p = os.path.join(cuda_home, 'lib', 'compat')
        return _p, cuda_home

    # Try NGC-style versioned path without trailing compat
    import glob
    candidates = sorted(glob.glob('/usr/local/cuda-*/compat'), reverse=True)
    if candidates:
        # Prefer NGC style where the library lives at <dir>/libcuda.so or just under <dir>
        return os.path.dirname(candidates[0]), os.path.dirname(os.path.dirname(candidates[0]))

    # Check if LD_LIBRARY_PATH already includes something CUDA-ish
    ld = os.environ.get('LD_LIBRARY_PATH', '')
    found = [p for p in ld.split(':') if 'cuda' in p.lower() and 'compat' in p.lower()]
    if found:
        return found[0].rstrip('/'), os.path.commonpath(found) + '/..'

    return None, None


def _inject_cuda_paths():
    """Prepend auto-detected CUDA paths to LD_LIBRARY_PATH."""
    compat_dir, cuda_home = _probe_cuda_libs()
    if compat_dir:
        env_val = f"{compat_dir}:{os.environ.get('LD_LIBRARY_PATH', '')}"
        os.environ['CUDA_HOME'] = cuda_home or os.path.dirname(compat_dir)
        # Only inject if not already set by cell-level command
        if 'CUDA13_INJECTED' not in os.environ:
            os.environ['LD_LIBRARY_PATH'] = env_val
            os.environ['CUDA13_INJECTED'] = '1'
    return compat_dir, cuda_home


_compat, _cuda_home = _inject_cuda_paths()

# ---------------------------------------------------------------------------
# Apply patches needed by openwakeword on modern systems
# ---------------------------------------------------------------------------

# Patch torchaudio.info (avoids librosa fallback issues)
import torchaudio
import soundfile

def patched_info(path):
    with soundfile.SoundFile(path) as sf:
        return type('Info', (), {'num_frames': sf.frames, 'sample_rate': sf.samplerate})()

torchaudio.info = patched_info

# Patch speechbrain convolve1d tensor indexing (rotation_index must be int, not tensor)
import torch
import speechbrain.processing.signal_processing as sp

_orig_cv1d = sp.convolve1d

def wrapped_cv1d(waveform, kernel, **kw):
    ri = kw.get('rotation_index', 0)
    if isinstance(ri, torch.Tensor):
        ri = ri.item() if ri.numel() == 1 else int(ri.flatten()[0].item())
        kw['rotation_index'] = ri
    return _orig_cv1d(waveform, kernel, **kw)

sp.convolve1d = wrapped_cv1d

# Suppress debug logging from onnxscript (causes massive slowdown / verbose output)
import logging
logging.getLogger('onnxscript').setLevel(logging.WARNING)
logging.getLogger('onnx_ir').setLevel(logging.WARNING)
logging.getLogger('torch.onnx').setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# OUTPUT filter - replaces inline NotebookOutputFilter entirely.
# All subsequent calls to sys.stdout write through this single class.
# ---------------------------------------------------------------------------

import threading as _threading

_BULK_EVERY = 100  # emit full line every N writes; partials use \r + clear-to-EOL

class OutputFilter:
    """Thin stream wrapper that coalesces per-batch output into single-line status lines.

    - Writes via carriage return (\\r) so each update overwrites the same line.
    - ANSI \\x1b[K clears remaining characters so previous longer lines don't leave ghost text.
    - Emits a newline after every ``_BULK_EVERY`` writes to avoid filling stdout buffers.
    """
    def __init__(self, raw_stream):
        self.raw = raw_stream
        self.phase_count = 0
        self._prev_phase = None
        self._phase_buf = ""
        self.CLREOL = chr(27) + "[K"   # clear from cursor to end of line
        self.CR  = chr(13)              # carriage return

    def write(self, text):
        self.phase_count += 1
        phase_label = str(getattr(self, '_prev_phase', ''))
        stripped = text.strip()

        # Detect phase changes (e.g., "Generating positive clips" → "Augmenting with RIRs")
        new_phase = None
        if stripped:
            if any(stripped.startswith(p) for p in ('Step', 'Epoch', 'Train', 'Augment',
                                                       'Positive', 'Negative', 'Batch',
                                                       'Processing')):
                new_phase = stripped.split(',')[0].split(':')[0]
            elif 'batch' in stripped.lower() and 'processed' in stripped.lower():
                new_phase = 'batch'

        if new_phase and new_phase != self._prev_phase:
            self.raw.write('\n')
            self._prev_phase = new_phase
            self._phase_buf = ""

        if not stripped:
            self._phase_buf += text
            return

        # Truncate long lines to avoid terminal overflow
        display_text = stripped.rstrip(' \t\r\n.,')
        if len(display_text) > 80:
            display_text = display_text[:77] + '...'

        summary = f"[{phase_label} {self.phase_count}/{_BULK_EVERY}] {display_text}"

        if self.phase_count % _BULK_EVERY == 0:
            # Emit complete line, flush buffer
            self.raw.write(self.CR + summary.ljust(80) + '\n')
            if self._phase_buf:
                self.raw.write(self._phase_buf)
                self._phase_buf = ""
        else:
            # Partial update - rewrites same line + clears ghost text
            self.raw.write(self.CR + self.CLREOL + summary.ljust(80) + ' ')

        if self.phase_count % 50 == 0:
            self.raw.flush()

    def flush(self):
        if self._phase_buf:
            self.raw.write(self._phase_buf)
            self._phase_buf = ""
        self.raw.write('\n')
        self.raw.flush()

    def isatty(self):
        return getattr(self.raw, 'isatty', lambda: False)()


# Apply output filter early so all print/write goes through it
if hasattr(sys.stdout, 'raw'):
    sys.stdout = OutputFilter(sys.stdout.raw)
else:
    sys.stdout = OutputFilter(sys.stdout)

print(f"OutputFilter applied (reports every {_BULK_EVERY} writes)")

# ---------------------------------------------------------------------------
# Main: dispatch to generate / augment / train sub-tasks
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Process and train openWakeWord custom model')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--generate_clips', action='store_true', help='Generate positive/negative audio clips')
    group.add_argument('--augment_clips',  action='store_true', help='Augment clips with RIRs and background noise')
    group.add_argument('--train_model',    action='store_true', help='Train the custom verifier model')
    parser.add_argument('--training_config', required=True, help='Path to training config YAML')
    args = parser.parse_args()

    # Build sys.path from available paths (always include these)
    for p in ['/home/jovyan/openwakeword', '/home/jovyan/piper-sample-generator']:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    # Resolve train.py - try local paths first, fall back to installed package
    train_path = None
    for candidate in [
        '/home/jovyan/openwakeword/openwakeword/train.py',
        '/home/jovyan/openwakeword/train.py',
        '/home/jovyan/piper-sample-generator/train.py',
    ]:
        if os.path.exists(candidate):
            train_path = candidate
            break

    if not train_path:
        import importlib.util as _iou
        spec = _iou.find_spec('openwakeword')
        if spec:
            pkg_dir = os.path.dirname(spec.origin)
            for c in ['train.py', 'openwakeword/train.py']:
                full = os.path.join(pkg_dir, c)
                if os.path.exists(full):
                    train_path = full
                    break

    if not train_path:
        # Last resort: try import and exec inline
        from openwakeword import train as _train_mod
        exec(compile(open('/home/jovyan/openwakeword/openwakeword/train.py').read(), 'train.py', 'exec'),
             {'__name__': '__main__', '__file__': 'train.py', '__builtins__': __builtins__})
        return

    print(f"Loading train.py: {train_path}")

    # Execute the training script
    with open(train_path) as _f:
        _code = _f.read()

    _ns = {
        '__name__': '__main__',
        '__file__': train_path,
        '__builtins__': __builtins__,
    }

    # Pass the right sys.argv so openWakeWord picks up the correct --config key
    if args.generate_clips:
        sys.argv = [train_path, '--training_config', args.training_config]
        print("Generating clips (Step 4a)...")
    elif args.augment_clips:
        sys.argv = [train_path, '--training_config', args.training_config, '--augment']
        print("Augmenting clips (Step 4b)...")
    else:
        sys.argv = [train_path, '--training_config', args.training_config, '--train']
        print(f"Training model (Step 5a)...\n")

    exec(_code, _ns)


if __name__ == '__main__':
    main()
