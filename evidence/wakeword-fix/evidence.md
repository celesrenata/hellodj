# Wake-Word Model Fix — Verification Evidence

**Date (UTC):** 2026-08-13T07:42Z
**Operator:** DevOps automation

## Path Taken

**Option B (NFS population, no image rebuild):**
- The NFS models share `nfs://192.168.42.8:/volume1/Kubernetes/HelloDJ/models` was populated
  out-of-band with `Hello_DJ.onnx` (9,487,501 bytes) by the operator.
- SSH to `192.168.42.8` was attempted first (path 1) but denied (no credentials), so the
  file was placed directly on the NFS share (equivalent of path 2 — out-of-band population).
- No Docker image rebuild was required. `kube/bot-configmap.yaml` was NOT modified
  (`WAKE_WORD_MODEL_PATH: /app/models/Hello_DJ.onnx` already correct).

## Actions

1. Confirmed model present on NFS mount inside pod:
   - `kubectl exec -n hellodj-service <pod> -c bot -- ls -la /app/models/` → `Hello_DJ.onnx` 9,487,501 bytes
2. `kubectl rollout restart deployment/hellodj -n hellodj-service` → `deployment.apps/hellodj restarted`
3. `kubectl rollout status deployment/hellodj -n hellodj-service --timeout=120s` → `deployment "hellodj" successfully rolled out`
4. New pod `hellodj-555558d954-fq7m2` verified: model present at `/app/models/Hello_DJ.onnx`

## Verification Output (new pod logs)

```
INFO:cogs.voice:VOICE_ENABLED=true — voice activation auto-enabled for all guilds
INFO:voice.wakeword:Wake word model loaded (/app/models/Hello_DJ.onnx) — input onnx::Flatten_0, providers=['CPUExecutionProvider']
INFO:voice.tts:speaches TTS engine configured (url=http://speaches.speaches-service.svc.cluster.local:8000)
INFO:cogs.voice:Voice orchestrator initialized (wakeword=True, tts=True, query=False)
INFO:cogs.voice:Voice tick loop started (every 80ms)
INFO:cogs.voice:Voice activation cog loaded
```

## Before (pre-fix pod logs)

```
WARNING:voice.wakeword:Wake word model not found at /app/models/Hello_DJ.onnx — voice activation will be disabled
WARNING:cogs.voice:Wake word model not found — voice activation disabled. Set WAKE_WORD_MODEL_PATH or place Hello_DJ.onnx in /app/models/
INFO:cogs.voice:Voice orchestrator initialized (wakeword=False, tts=True, query=False)
```

## Conclusion

- ✅ `/app/models/Hello_DJ.onnx` present (9,487,501 bytes) in running pod
- ✅ Log no longer reports "Wake word model not found"
- ✅ Wake word model loaded successfully (ONNX, CPUExecutionProvider)
- ✅ Voice orchestrator initialized with `wakeword=True` → voice listening active
