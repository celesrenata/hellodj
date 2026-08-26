# hls-transcode

The `hls-transcode` component of the HelloDJ AWS platform.

## Responsibility

- Perform **HLS transcode** for the Discord Activity with a **hybrid CPU/GPU
  path** (Decision D3, the "gas/electric" model):
  - **libx264 on Graviton** is the always-available floor that serves every
    interactive request immediately and covers the GPU spin-up window, so the
    ≤5s Interactive_Latency_Budget holds (Requirements 3.1, 3.9).
  - **`h264_nvenc`** on a warm, time-sliced G5g node is preferred only while the
    shared hybrid-GPU controller reports the GPU `Ready` (Requirement 3.11).
- Render the **audio visualizer** frames fed to the encoder, preserving the
  visualizer feature (Requirement 6.2).
- **Consume media over loopback/intra-node** from the co-located producers
  (lavalink / activity-backend) so the producer → transcoder hop is free
  (Decision D2).
- **Write HLS to S3** (the CloudFront origin) for viewers (Requirements 18.2,
  18.4).
- **Publish CPU/GPU pressure metrics to CloudWatch** for the Autoscaler
  (Requirement 16.4).

It is an independently deployable, independently versioned component
(Requirement 15.1): its own Nix-built image, its own semantic version, and its
own CI/CD path.

## Package layout

```
hls_transcode/
├── __init__.py       # package version + public exports
├── config.py         # environment-driven runtime settings + hybrid thresholds
├── encoder.py        # libx264/NVENC selection + HLS (fMP4/TS) command builder
├── scheduler.py      # hybrid-GPU-driven scheduler (consumes hybrid_gpu.py)
├── hls_writer.py     # local scratch layout + S3 key / CloudFront URL derivation
├── s3_sink.py        # S3 upload sink (injectable/lazy boto3) — CloudFront origin
├── metrics.py        # CloudWatch put_metric_data publisher (injectable/lazy)
├── visualizer.py     # visualizer frame-source hook (R6.2)
├── jobs.py           # job planning/tracking over the pure surfaces
└── server.py         # aiohttp /v1/transcode start/stop service + entry point
```

## Hybrid gas/electric model

Encoder selection is a pure function of the shared
`hellodj_platform_logic.hybrid_gpu` controller state:

| Controller state | `gpu_preferred` | Encoder (GPU node group present) |
|---|---|---|
| `ELECTRIC_ONLY` | false | libx264 |
| `ENGINE_STARTING` (GPU spinning up) | false | libx264 (covers boot window) |
| `HYBRID_GPU` (GPU Ready) | true | **NVENC** |
| `COASTING` | false | libx264 |

When no GPU node group is provisioned (`HELLODJ_GPU_AVAILABLE` unset), the
scheduler stays on libx264 unconditionally — the software-transcode-only default
(Requirement 3.9).

## Interfaces

- **activity-backend** — calls `POST /v1/transcode` and `POST /v1/transcode/stop`
  over intra-node loopback to start/stop jobs (Decision D2, Requirement 18.4).
- **S3 + CloudFront** — HLS playlists/segments are written to S3 via
  `hls_transcode.s3_sink.S3Sink` and served through CloudFront.
- **CloudWatch** — CPU/GPU pressure published via
  `hls_transcode.metrics.PressureMetrics` for the Autoscaler (Requirement 16.4).

## Configuration (environment)

| Variable | Purpose | Default |
|---|---|---|
| `HELLODJ_HLS_S3_BUCKET` | S3 bucket for HLS output (CloudFront origin) | (empty) |
| `HELLODJ_HLS_S3_PREFIX` | key prefix for HLS objects | `hls` |
| `HELLODJ_CLOUDFRONT_DOMAIN` | CloudFront domain for viewer URLs | (empty) |
| `HELLODJ_METRICS_NAMESPACE` | CloudWatch namespace | `HelloDJ/Transcode` |
| `HELLODJ_GPU_AVAILABLE` | whether a GPU node group exists | `false` |
| `HELLODJ_GPU_SPIN_UP` / `HELLODJ_GPU_SPIN_DOWN` | hybrid thresholds | `0.80` / `0.30` |
| `HELLODJ_GPU_SPIN_UP_WINDOW_S` / `HELLODJ_GPU_SPIN_DOWN_WINDOW_S` | sustained windows | `30` / `120` |
| `HELLODJ_HLS_SEGMENT_S` | HLS segment duration | `2.0` |
| `HELLODJ_TRANSCODE_HOST` / `HELLODJ_TRANSCODE_PORT` | bind host/port | `0.0.0.0` / `8080` |
| `AWS_REGION` | region for AWS SDK clients | boto3 default chain |

## Development

```bash
# From the platform root:
uvx ruff@0.6.9 check components/hls-transcode
python3 tools/check_line_count.py components/hls-transcode

# aiohttp / boto3 may not be installed in every environment; the modules are
# import-structured (lazy imports) so syntax can be verified without them:
python3 -m py_compile components/hls-transcode/hls_transcode/*.py

# Unit tests exercise the pure surfaces (config, encoder selection following the
# controller state, HLS command building, scheduler, HLS layout/keys, S3 sink +
# CloudWatch metrics with fake clients, visualizer frames, handlers):
PYTHONPATH=components pytest components/hls-transcode/tests
```
