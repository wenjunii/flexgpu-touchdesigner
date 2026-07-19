# Optional Depth Anything sensor emulator

This integration temporarily turns a laptop webcam into the audience-sensor
boundary while no physical depth sensor is available. It is optional and
default-off. It is **not** the generated-world depth path: MoGe-2 reconstructs
the StreamDiffusion image, while this worker estimates only audience
interaction depth.

The camera and model run in an isolated external process. Camera RGB remains
in volatile process memory. The worker has no RGB recording path and sends
only pseudo-depth, foreground mask, and heuristic confidence to
TouchDesigner.

## Immutable model choice

The optional real backend pins the official Transformers-compatible
`depth-anything/Depth-Anything-V2-Small-hf` snapshot:

- revision `870a35c76c2bc1d82fbde922d95015496cb7dd6c`;
- `model.safetensors` size `99173660` bytes;
- SHA-256 `3152477ce0d8d6978d76b995120de97cb5b928701fd0f817769f59e249a16b70`.

The upstream project and model card label **V2 Small** Apache-2.0. Upstream
labels Base/Large/Giant CC-BY-NC-4.0, so this integration deliberately does
not select those models. Review upstream terms and your installation's data
policy before deployment. The public repository does not redistribute model
weights.

## Preview, install, and acquire the model

All scripts are preview-first:

```powershell
.\scripts\Initialize-DepthAnything.ps1
.\scripts\Initialize-DepthAnything.ps1 -Install
```

Model acquisition is a separate, explicit network action:

```powershell
.\scripts\Initialize-DepthAnything.ps1 -DownloadModel
```

The environment lives under ignored `.venv/depth-anything/`; the snapshot and
cache remain under ignored `runtime/`. Normal inference is forced offline and
verifies the pinned weight before loading it.

## Rehearse without a webcam or model

Start the default-off TouchDesigner receiver first. Then preview and run the
deterministic mock:

```powershell
.\scripts\Start-DepthAnythingWorker.ps1 -Backend mock
.\scripts\Start-DepthAnythingWorker.ps1 `
  -Backend mock `
  -CalibrationFrames 1 `
  -MaxFrames 3 `
  -Start
```

If the optional isolated runtime has not been installed, mock mode falls back
to `python` on `PATH` and needs only NumPy. It does not load a model or open the
webcam.

`-Backend mock -Capture webcam` exercises the real camera boundary with a
deterministic depth backend. The webcam still does not open until `-Start` is
present. `-CameraBackend auto` is the default; it prefers MSMF on Windows and
the OpenCV default backend elsewhere. Explicit `msmf`, `dshow`, and `any`
values are available for bounded device diagnosis. Worker JSON reports the
requested/selected backend and camera-open milliseconds without including RGB.
The worker verifies the receiver before camera open, refreshes the result
connection after a slow backend open, and only then starts reading camera frames.

For the initial RTX 3080 Ti Laptop test:

```powershell
.\scripts\Start-DepthAnythingWorker.ps1 `
  -Profile 3080ti_16gb `
  -GpuIndex 0 `
  -CameraIndex 0 `
  -CameraBackend auto `
  -Start
```

The live-accepted 3080 Ti Laptop rehearsal default is ViT-S fp16, 640x480
webcam capture, 384 model input, 256x144 sensor output, and 5 Hz inference.
Use 3 Hz as the first fallback if the combined StreamDiffusion + MoGe +
interaction workload is thermally unstable. Producer and receiver share a hard
640x480 / 307200-pixel output ceiling. Results use TCP 9241 only; UDP 9240 is
reserved metadata and is not opened. Both the worker and receiver require an
independent explicit trusted-network opt-in before using non-loopback hosts.

## Replace it later without rewiring the world

A paid Depth Anything app, physical depth camera, or vendor sensor service can
replace this worker. The replacement should emit the same bounded sensor
contract documented in [DEPTH_ANYTHING_SENSOR.md](../../docs/DEPTH_ANYTHING_SENSOR.md).
The TouchDesigner audience-interaction adapter and downstream world do not
need to know which backend produced the depth.
