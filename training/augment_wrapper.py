#!/usr/bin/env python3
"""Wrapper: patches torchaudio.info and speechbrain convolve1d, then runs train.py."""
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

# Add paths
sys.path.insert(0, '/home/jovyan/openwakeword')
sys.path.insert(0, '/home/jovyan/piper-sample-generator')

# Run train.py
train_path = '/home/jovyan/openwakeword/openwakeword/train.py'
with open(train_path) as f:
    code = f.read()

ns = {
    '__name__': '__main__',
    '__file__': train_path,
    '__builtins__': __builtins__,
}
sys.argv = [train_path] + sys.argv[1:]
exec(code, ns)
