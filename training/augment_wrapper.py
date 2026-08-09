#!/usr/bin/env python3
"""Wrapper: patches torchaudio.info and speechbrain convolve1d, then runs train.py.

Batch output is reduced: prints status every N iterations as a single line update
using carriage return (\\r) for live output that doesn't create new lines.
"""
import sys
import os

# Patch torchaudio.info
import torchaudio
import soundfile

def patched_info(path):
    with soundfile.SoundFile(path) as sf:
        return type('Info', (), {'num_frames': sf.frames, 'sample_rate': sf.samplerate})()

torchaudio.info = patched_info

# Patch speechbrain convolve1d tensor index
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

# Suppress debug logging from onnxscript (massive slowdown)
import logging
logging.getLogger('onnxscript').setLevel(logging.WARNING)
logging.getLogger('onnx_ir').setLevel(logging.WARNING)
logging.getLogger('torch.onnx').setLevel(logging.WARNING)

# ============================================================
# Reduced batch output filter
# ============================================================
# Wraps stdout so that batch iteration prints are coalesced:
#   - All writes go through summary logic (no bypass for newlines)
#   - Uses carriage return (\\r) so updates rewrite the SAME line
#   - Progress updates accumulate on one line until flushed
# ============================================================
_BATCH_OUTPUT_ENABLED = True  # Set False to disable all reduced output logic
_BATCH_EVERY = 100            # Report every N iterations

if _BATCH_OUTPUT_ENABLED:
    class OutputFilter:
        """Filter batch output: coalesce multiple tiny prints into every_n status lines."""
        def __init__(self, raw_stream):
            self.raw = raw_stream
            self.phase_count = 0
            self._prev_phase = None

        def write(self, text):
            self.phase_count += 1

            # Detect phase change by looking for key prefixes
            new_phase = None
            stripped = text.strip()
            if stripped:
                if stripped.startswith(('Step', 'Epoch', 'Train', 'Augment', 'Positive', 'Negative', 'Batch', 'Processing')):
                    new_phase = stripped.split(',')[0].split(':')[0]
                elif 'step' in stripped.lower() and (' ' in stripped or stripped.rstrip('0123456789.,').endswith('step')):
                    new_phase = 'step'
                elif 'batch' in stripped.lower() and 'processed' in stripped.lower():
                    new_phase = 'batch'

            # Phase boundary: end previous line with \\n
            if new_phase and new_phase != self._prev_phase:
                self.raw.write('\n')
                self._prev_phase = new_phase

            # Collect meaningful text (ignore bare newlines/carriage returns)
            # Always go through summary logic (don't bypass via raw.write)
            phase_label = str(getattr(self, '_prev_phase', 'batch'))
            if not stripped:
                display_text = ""
            else:
                # Strip trailing dots, commas, spaces, carriage returns
                display_text = stripped.rstrip(' \t\r\n.,')
            if len(display_text) > 80:
                display_text = display_text[:77] + '...'
            
            # Track whether we hit a natural boundary (every _BATCH_EVEN iterations)
            should_emit = self.phase_count % _BATCH_EVERY == 0
            
            # Write summary: \\r moves cursor to start of line (overwrites in place)
            # When emitting: add \\n to finish the line
            # When not emitting: no \\n so subsequent writes stay on same line
            if should_emit:
                # Flush: write summary AND finish the line
                summary = f"[{phase_label} {self.phase_count}/{_BATCH_EVERY}] {display_text}"
                self.raw.write('\r' + summary.ljust(80) + '\n')
            elif stripped:
                # Partial update: clear line and rewrite (removes ghost text)
                CLREOL = chr(27) + "[K"  # ANSI clear to end of line
                summary = f"[{phase_label} {self.phase_count}/{_BATCH_EVERY}] {display_text}"
                self.raw.write(chr(13) + CLREOL + summary.ljust(80) + ' ')
            # If no stripped text but not emitting: do nothing (keep current state)

            self.raw.flush()

        def flush(self):
            # Terminate any active summary line
            self.raw.write('\n')
            self.raw.flush()

        def isatty(self):
            return getattr(self.raw, 'isatty', lambda: True)()

    # Apply the filter to stdout
    if not isinstance(getattr(sys.stdout, '_orig', sys.stdout), OutputFilter):
        _filtered = OutputFilter(sys.stdout)
        sys.stdout._orig = sys.stdout
        sys.stdout = _filtered

# Add paths
sys.path.insert(0, '/home/jovyan/openwakeword')
sys.path.insert(0, '/home/jovyan/piper-sample-generator')

# Find train.py with fallback
train_path = None
for p in [
    '/home/jovyan/openwakeword/openwakeword/train.py',
    '/home/jovyan/openwakeword/train.py',
    '/home/jovyan/piper-sample-generator/train.py',
]:
    if os.path.exists(p):
        train_path = p
        break
if not train_path:
    train_path = '/home/jovyan/openwakeword/openwakeword/train.py'

# Run train.py
with open(train_path) as f:
    code = f.read()

ns = {
    '__name__': '__main__',
    '__file__': train_path,
    '__builtins__': __builtins__,
}
sys.argv = [train_path] + sys.argv[1:]
exec(code, ns)

# Flush any remaining buffered output
if isinstance(sys.stdout, OutputFilter):
    sys.stdout.flush()
