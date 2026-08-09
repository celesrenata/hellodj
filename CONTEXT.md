# Training Pipeline Context

## Goal
Train a custom wake word model for "Hello DJ" using openWakeWord.
- Target phrase: "Hello DJ"
- Model name: Hello_DJ
- Scale: 200k positive samples, 50k training steps

## Current State
- Notebook: `training/training.ipynb` (at `/home/jovyan/hellodj/training/training.ipynb`)
- Config: `/home/jovyan/my_custom_model.yml`
- augment_wrapper.py: DELETED - all logic moved inline into notebook cells
- scipy: pinned to <1.17 (1.13.1) to keep sph_harm
- Kernel: ready to restart

## Training Pipeline
- Step 1: Download MIT RIRs (environment) + background audio datasets
- Step 2: Download pre-computed openWakeWord features (Numpy files)
- Step 3: Install openwakeword, patch piper, download model config
- Step 4a: Generate positive samples (Piper TTS)
- Step 4b: Augment clips with RIRs + background noise
- Step 5a: Train the model (train_custom_verifier)
- Step 5b: Verify outputs (ONNX/TFLite)

## Key Architecture
```
training/training.ipynb
├── Cell 1: "InlineOutputFilter" class (the output filter)
├── Cell 9  : Step 4a - generate positive/negative clips (inline)
├── Cell 10 : Step 4b - augment with RIRs+noise  (inline)
└── Cell 13 : Step 5a - train the model           (inline)
```

## Output Filter Details
The InlineOutputFilter (Cell 1) solves the batch iteration output problem:
- Wraps sys.stdout so all writes go through the filter
- Uses `\r + \x1b[K` (carriage return + ANSI clear-to-end-of-line) to update the SAME line
- Reports every 100 iterations as a single status line
- Ghost text is removed (no leftover characters from longer previous lines)
- NEWLINES no longer bypass the filter (previously the major issue)

## Key File Paths
```
/home/jovyan/hellodj/training/training.ipynb   - main notebook
/home/jovjan/my_custom_model.yml               - training config
/home/jovyan/openwakeword/                     - openwakeword source (cloned in Step 3a)
/home/jovyan/piper-sample-generator/           - Piper sample generation (cloned in Step 3a)
/home/jovyan/Hello_DJ/                         - output directory for model files
/home/jovyan/mit_rirs/16khz/                   - MIT impulse responses
/tmp/audioset_16k/                             - background noise audio
```

## Common Issues
1. **scipy >1.17** breaks `sph_harm` import - pinned to <1.17
2. **augment_wrapper.py deleted** - must use inline cells in notebook
3. **train.py path** - falls back to installed package when local dirs missing
4. **OutputFilter bypass for newlines** - old text from downloading bytes created duplicate lines
5. **Ghost text** - `\r` alone doesn't clear when previous line was longer - fixed with `\x1b[K`

## Recent Commits
```
6c13288 put OutputFilter inline in notebook (no augment_wrapper.py dependency)
7b5d1a1 remove augment_wrapper.py, put OutputFilter inline
b2fa1fb put OutputFilter inline in notebook cells
f243e07 fix OutputFilter ghost text: use ANSI clear-to-end-of-line
abbead9 reset notebook output, delete all downloads, restart kernel
9454e90 fix OutputFilter to always go through summary logic
```

## Next Steps
1. Restart kernel
2. Run notebook cells in order (1 through 5b)
3. Verify Step 4/5 output is clean (single line updates, not verbose)
