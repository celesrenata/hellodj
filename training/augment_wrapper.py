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
    # Our training dir is first so webrtcvad.py shim overrides system package
    our_dir = os.path.dirname(os.path.abspath(__file__))
    for p in [our_dir, '/home/jovyan/openwakeword', '/home/jovyan/piper-sample-generator']:
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
