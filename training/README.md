# Hello DJ — Wake Word Training

Custom "Hello DJ" wake word model for a Discord music bot with voice activation.

## Requirements

- GPU with CUDA (tested on RTX 4070 Ti SUPER, 16.7 GB VRAM)
- PyTorch 2.13, torchaudio 2.11, speechbrain 1.1.0

## Setup

```bash
git clone git@github.com:celesrenata/openWakeWord.git -b hello-dj-training
git clone git@github.com:celesrenata/piper-sample-generator.git -b hello-dj-training
```

## Usage

Open `training.ipynb` in Jupyter and run cells in order (Steps 1–5).

## Output

- ONNX model: `/home/jovyan/Hello_DJ/Hello_DJ.onnx`
- Training clips: 200k positive + 200k negative

## Patches

- `augment_wrapper.py`: patches torchaudio.info, speechbrain convolve1d, onnxscript logging
- openWakeWord `data.py`: stereo RIR → mono + 44.1kHz→16kHz resampling
- openWakeWord `train.py`: opset_version=17, dynamo=False for ONNX export
- piper-sample-generator `generate_samples.py`: weights_only=False for torch.load
