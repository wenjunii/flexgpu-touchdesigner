"""Build the built-in, immediately visible FlexGPU runtime pipeline.

This module is intentionally safe to import outside TouchDesigner.  Call
``build(op('/project1/flexgpu'))`` from a TouchDesigner Textport or DAT to
create/update only ``WORKING_PIPELINE``.  The builder uses stock TouchDesigner
2025 operators and never destroys nodes.

The demo generators make the branch useful before any model is installed.
Later, replace the two clearly labelled TOPs in ``STREAMDIFFUSION_ADAPTER``
with the RGB/depth outputs of ``StreamDiffusionTD.tox``; every downstream
contract remains unchanged.
"""

from __future__ import print_function

import os


BUILD_VERSION = "1.2.1"
ROOT_PATH = "/project1/flexgpu"
PIPELINE_NAME = "WORKING_PIPELINE"


# These names are deliberately public and covered by source tests.  They form
# the stable integration surface shared by the demo, StreamDiffusionTD, the
# point renderer, installation output, and a later headset-specific renderer.
TOP_CONTRACTS = {
    "RGB": "RGBA color TOP; linear or sRGB, alpha=1",
    "DEPTH": "R raw depth TOP; encoding/scale/bias are defined by calibration",
    "CONFIDENCE": "R confidence/validity TOP normalized 0..1 and aligned with DEPTH",
    "POSITION": "RGBA32F TOP; RGB=XYZ metres, A=active/valid",
    "COLOR": "RGBA16F or RGBA8 TOP aligned pixel-for-pixel with POSITION",
    "TEMPORAL_STATE": "RGBA16F TOP; R=confidence, G=normalized age, B=current-valid, A=alive",
    "SENSOR_POSITION": "RGBA32F TOP; RGB=sensor-local XYZ metres, A=occupancy/confidence",
    "INTERACTION": "RGBA16F TOP; RGB=force vector, A=occupancy",
    "INSTALLATION": "RGBA TOP; visually inspectable rendered point world",
    "STEREO": "two eye RGBA TOPs plus a side-by-side preview",
}


EXPERIMENTAL_ADAPTERS = {
    "SHARP_EXTERNAL": {
        "default_enabled": False,
        "contract": "External process publishes POSITION and COLOR TOPs.",
    },
    "GAUSSIAN_EXTERNAL": {
        "default_enabled": False,
        "contract": "External process publishes a rendered RGBA view or POSITION/COLOR TOPs.",
    },
}


# Pixel shaders use only TouchDesigner GLSL TOP built-ins.  Keeping the source
# strings at module level makes the GPU contracts reviewable without opening a
# .toe and lets CI guard against accidental interface drift.
SHADERS = {
    "point_glyph": r'''// CONTRACT: sprite UV -> soft circular white glyph
out vec4 fragColor;

void main()
{
    vec2 pointUV = vUV.st * 2.0 - 1.0;
    float radius = length(pointUV);
    float alpha = 1.0 - smoothstep(0.72, 1.0, radius);
    fragColor = TDOutputSwizzle(vec4(1.0, 1.0, 1.0, alpha));
}
''',
    "validity_combine": r'''// CONTRACT: MASK + CONFIDENCE -> CONFIDENCE
out vec4 fragColor;

void main()
{
    float mask = clamp(texture(sTD2DInputs[0], vUV.st).r, 0.0, 1.0);
    float confidence = clamp(texture(sTD2DInputs[1], vUV.st).r, 0.0, 1.0);
    float validity = mask * confidence;
    fragColor = TDOutputSwizzle(vec4(validity, validity, validity, 1.0));
}
''',
    "depth_to_position": r'''// CONTRACT: RGB + DEPTH + CONFIDENCE -> POSITION (world XYZ metres + active alpha)
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    float rawDepth = texture(sTD2DInputs[1], uv).r;
    float confidence = clamp(texture(sTD2DInputs[2], uv).r, 0.0, 1.0);
    const int depthMode = 0; // FLEXGPU_DEPTH_MODE: 0 normalized, 1 metric, 2 inverse
    const float depthScale = 1.0; // FLEXGPU_DEPTH_SCALE
    const float depthBias = 0.0; // FLEXGPU_DEPTH_BIAS
    const float nearMetres = 0.35; // FLEXGPU_NEAR_METRES
    const float farMetres = 4.50; // FLEXGPU_FAR_METRES
    // Normalized intrinsics are measured in full image widths/heights. Zero
    // focal values retain the original 60 degree, aspect-aware demo camera.
    const float fxNormalized = 0.0; // FLEXGPU_INTRINSICS_FX
    const float fyNormalized = 0.0; // FLEXGPU_INTRINSICS_FY
    const float cxNormalized = 0.5; // FLEXGPU_INTRINSICS_CX
    const float cyNormalized = 0.5; // FLEXGPU_INTRINSICS_CY
    const vec4 cameraToWorld0 = vec4(1.0, 0.0, 0.0, 0.0); // FLEXGPU_CAMERA_TO_WORLD_0
    const vec4 cameraToWorld1 = vec4(0.0, 1.0, 0.0, 0.0); // FLEXGPU_CAMERA_TO_WORLD_1
    const vec4 cameraToWorld2 = vec4(0.0, 0.0, 1.0, 0.0); // FLEXGPU_CAMERA_TO_WORLD_2
    const vec4 cameraToWorld3 = vec4(0.0, 0.0, 0.0, 1.0); // FLEXGPU_CAMERA_TO_WORLD_3

    float calibrated = rawDepth * depthScale + depthBias;
    float z = mix(nearMetres, farMetres, clamp(calibrated, 0.0, 1.0));
    if (depthMode == 1) {
        z = calibrated;
    } else if (depthMode == 2) {
        z = 1.0 / max(calibrated, 1e-6);
    }
    float aspect = float(textureSize(sTD2DInputs[1], 0).x) /
                   max(1.0, float(textureSize(sTD2DInputs[1], 0).y));
    float fx = fxNormalized > 1e-6 ? fxNormalized : 0.86602540378 / aspect;
    float fy = fyNormalized > 1e-6 ? fyNormalized : 0.86602540378;
    vec3 cameraPosition = vec3((uv.x - cxNormalized) * z / fx,
                               -(uv.y - cyNormalized) * z / fy,
                               -z);
    vec4 homogeneous = vec4(cameraPosition, 1.0);
    vec3 worldPosition = vec3(dot(cameraToWorld0, homogeneous),
                              dot(cameraToWorld1, homogeneous),
                              dot(cameraToWorld2, homogeneous));
    bool depthValid = depthMode == 0
        ? (calibrated >= 0.0 && calibrated <= 1.0)
        : (z >= nearMetres && z <= farMetres);
    // Depth endpoints are real near/far samples. Invalidity belongs in the
    // explicit mask/confidence contract, not in arbitrary depth cutoffs.
    float valid = float(depthValid) * float(confidence > 0.0);
    fragColor = TDOutputSwizzle(vec4(worldPosition, valid * confidence));
}
''',
    "sensor_position": r'''// CONTRACT: SENSOR MASK -> SENSOR_POSITION
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    float occupancy = texture(sTD2DInputs[0], uv).r;
    vec3 position = vec3((uv.x - 0.5) * 3.0,
                         (0.5 - uv.y) * 2.0,
                         -1.15 - 0.20 * occupancy);
    fragColor = TDOutputSwizzle(vec4(position, occupancy));
}
''',
    "sensor_to_world": r'''// CONTRACT: SENSOR_POSITION camera XYZ -> calibrated world XYZ
out vec4 fragColor;

void main()
{
    vec4 sensor = texture(sTD2DInputs[0], vUV.st);
    const vec4 sensorToWorld0 = vec4(1.0, 0.0, 0.0, 0.0); // FLEXGPU_SENSOR_TO_WORLD_0
    const vec4 sensorToWorld1 = vec4(0.0, 1.0, 0.0, 0.0); // FLEXGPU_SENSOR_TO_WORLD_1
    const vec4 sensorToWorld2 = vec4(0.0, 0.0, 1.0, 0.0); // FLEXGPU_SENSOR_TO_WORLD_2
    const vec4 sensorToWorld3 = vec4(0.0, 0.0, 0.0, 1.0); // FLEXGPU_SENSOR_TO_WORLD_3
    vec4 homogeneous = vec4(sensor.rgb, 1.0);
    vec3 worldPosition = vec3(dot(sensorToWorld0, homogeneous),
                              dot(sensorToWorld1, homogeneous),
                              dot(sensorToWorld2, homogeneous));
    // Mask and confidence are applied exactly once by SENSOR_VALIDITY after
    // the simulated/replay/hardware position routes have converged.
    fragColor = TDOutputSwizzle(vec4(worldPosition, sensor.a));
}
''',
    "sensor_validity": r'''// CONTRACT: SENSOR_POSITION + MASK + CONFIDENCE -> valid SENSOR_POSITION
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 sensor = texture(sTD2DInputs[0], uv);
    float mask = clamp(texture(sTD2DInputs[1], uv).r, 0.0, 1.0);
    float confidence = clamp(texture(sTD2DInputs[2], uv).r, 0.0, 1.0);
    fragColor = TDOutputSwizzle(vec4(sensor.rgb, sensor.a * mask * confidence));
}
''',
    "interaction_field": r'''// CONTRACT: POSITION + calibrated SENSOR_POSITION -> INTERACTION force + occupancy
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 point = texture(sTD2DInputs[0], uv);
    const float interactionRadiusMetres = 0.55; // FLEXGPU_INTERACTION_RADIUS
    const float forceGain = 1.0; // FLEXGPU_FORCE_GAIN
    // Bounded 8x8 occupancy primitives are sampled across the full sensor,
    // not at the generated point's source UV. This is a practical stock-TD
    // approximation to a low-resolution world-space occupancy/SDF volume.
    const int occupancyGridSize = 8; // FLEXGPU_OCCUPANCY_GRID_SIZE
    vec3 accumulatedForce = vec3(0.0);
    float combinedOccupancy = 0.0;
    for (int y = 0; y < occupancyGridSize; ++y) {
        for (int x = 0; x < occupancyGridSize; ++x) {
            vec2 sensorUV = (vec2(float(x), float(y)) + 0.5) /
                            float(occupancyGridSize);
            vec4 sensor = texture(sTD2DInputs[1], sensorUV);
            vec3 delta = point.rgb - sensor.rgb;
            float distanceMetres = length(delta);
            float influence = point.a * sensor.a *
                (1.0 - smoothstep(0.0, interactionRadiusMetres, distanceMetres));
            vec3 direction = distanceMetres > 1e-5
                ? delta / distanceMetres : vec3(0.0, 0.0, 1.0);
            accumulatedForce += direction * influence;
            combinedOccupancy = max(combinedOccupancy, influence);
        }
    }
    vec3 force = accumulatedForce * max(0.0, forceGain) /
                 float(occupancyGridSize);
    fragColor = TDOutputSwizzle(vec4(force, combinedOccupancy));
}
''',
    "temporal_observation": r'''// CONTRACT: POSITION + CONFIDENCE + FRAME_CONTROL -> TEMPORAL_OBSERVATION
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 current = texture(sTD2DInputs[0], uv);
    float inputConfidence = clamp(texture(sTD2DInputs[1], uv).r, 0.0, 1.0);
    // R=new-frame one-cook pulse, G=bounded dt seconds, B=source valid,
    // A=maximum carried age seconds.
    vec4 control = texture(sTD2DInputs[2], vec2(0.5));
    float hasCurrent = step(0.001, current.a) * step(0.001, inputConfidence) *
                       step(0.5, control.r) * step(0.5, control.b);
    fragColor = TDOutputSwizzle(vec4(inputConfidence,
                                     clamp(control.g, 0.0, 0.25),
                                     hasCurrent,
                                     max(0.05, control.a)));
}
''',
    "temporal_state": r'''// CONTRACT: TEMPORAL_OBSERVATION + STATE_HISTORY -> TEMPORAL_STATE
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    // Observation pre-resolves POSITION + CONFIDENCE + FRAME_CONTROL so this
    // and every other stock GLSL TOP remains within TD 2025's three inputs.
    vec4 observation = texture(sTD2DInputs[0], uv);
    vec4 history = texture(sTD2DInputs[1], uv);
    const float confidenceDecay = 0.985; // FLEXGPU_CONFIDENCE_DECAY
    float inputConfidence = observation.r;
    float deltaSeconds = observation.g;
    float hasCurrent = observation.b;
    float maximumAgeSeconds = observation.a;
    float ageStep = deltaSeconds / maximumAgeSeconds;
    float age = mix(min(1.0, history.g + ageStep), 0.0, hasCurrent);
    float timeBasedRetention = pow(clamp(confidenceDecay, 0.0, 1.0),
                                   deltaSeconds * 60.0);
    float carriedConfidence = history.r * timeBasedRetention *
                              (1.0 - step(1.0, age));
    float confidence = max(hasCurrent * inputConfidence, carriedConfidence);
    float alive = step(0.001, confidence);
    fragColor = TDOutputSwizzle(vec4(confidence, age, hasCurrent, alive));
}
''',
    "temporal_advect": r'''// CONTRACT: HISTORY + INTERACTION + FRAME_CONTROL -> ADVECTED_HISTORY
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 history = texture(sTD2DInputs[0], uv);
    vec4 interaction = texture(sTD2DInputs[1], uv);
    vec4 control = texture(sTD2DInputs[2], vec2(0.5));
    // Force is metres/second. Clamp dt so a debugger pause cannot launch the
    // world when cooking resumes.
    float motionDt = min(max(control.g, 0.0), 1.0 / 15.0);
    float historyAlive = step(0.001, history.a);
    vec3 carried = history.rgb + interaction.rgb * motionDt * historyAlive;
    fragColor = TDOutputSwizzle(vec4(carried, history.a));
}
''',
    "temporal_persistence": r'''// CONTRACT: POSITION + ADVECTED_HISTORY + TEMPORAL_STATE -> PERSISTENT_POSITION
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 current = texture(sTD2DInputs[0], uv);
    vec4 history = texture(sTD2DInputs[1], uv);
    vec4 state = texture(sTD2DInputs[2], uv);
    const float newFrameBlend = 0.36;
    float hasCurrent = step(0.001, current.a) * state.b;
    float hasHistory = step(0.001, history.a);
    // Seed immediately on the first valid frame; low-pass only once history exists.
    float blend = hasCurrent * mix(1.0, newFrameBlend * state.r, hasHistory);
    vec3 position = mix(history.rgb, current.rgb, blend);
    float currentActivity = current.a * state.b;
    // state.r is the absolute confidence retained since the last accepted
    // frame. Multiplying it into feedback alpha would reapply all prior decay
    // on every cook, collapsing held 5-10 Hz frames super-exponentially.
    // Clamp the carried occupancy to that absolute envelope instead: this
    // applies decay once while state.a still enforces the configured lifetime.
    float carriedActivity = min(history.a, state.r);
    float activity = state.a * max(currentActivity, carriedActivity);
    fragColor = TDOutputSwizzle(vec4(position, activity));
}
''',
    "temporal_color": r'''// CONTRACT: COLOR + COLOR_HISTORY + TEMPORAL_STATE -> PERSISTENT_COLOR
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 currentColor = texture(sTD2DInputs[0], uv);
    vec4 historyColor = texture(sTD2DInputs[1], uv);
    vec4 state = texture(sTD2DInputs[2], uv);
    const float newColorBlend = 0.42;
    // state.b already contains the position-valid, confidence, source-valid,
    // and one-cook new-frame decision from TEMPORAL_OBSERVATION.
    float hasCurrent = state.b;
    float hasHistory = step(0.001, historyColor.a);
    float blend = hasCurrent * mix(1.0, newColorBlend * state.r, hasHistory);
    vec3 color = mix(historyColor.rgb, currentColor.rgb, blend);
    fragColor = TDOutputSwizzle(vec4(color, state.r));
}
''',
    "fog_completion": r'''// CONTRACT: PERSISTENT_POSITION + COLOR -> FOG_COLOR
out vec4 fragColor;

float hash21(vec2 p)
{
    p = fract(p * vec2(234.34, 435.345));
    p += dot(p, p + 34.23);
    return fract(p.x * p.y);
}

void main()
{
    const float fogDensity = 0.35; // FLEXGPU_FOG_DENSITY
    const float disocclusionRadius = 2.0; // FLEXGPU_DISOCCLUSION_RADIUS
    const float fogNoiseAmount = 0.50; // FLEXGPU_FOG_NOISE
    vec2 uv = vUV.st;
    vec4 position = texture(sTD2DInputs[0], uv);
    vec4 source = texture(sTD2DInputs[1], uv);
    vec2 texel = 1.0 / vec2(textureSize(sTD2DInputs[0], 0));
    vec2 radiusTexel = texel * max(1.0, disocclusionRadius);
    float nearby = max(max(texture(sTD2DInputs[0], uv + vec2(radiusTexel.x, 0.0)).a,
                           texture(sTD2DInputs[0], uv - vec2(radiusTexel.x, 0.0)).a),
                       max(texture(sTD2DInputs[0], uv + vec2(0.0, radiusTexel.y)).a,
                           texture(sTD2DInputs[0], uv - vec2(0.0, radiusTexel.y)).a));
    float disocclusion = nearby * (1.0 - position.a);
    float noiseFog = smoothstep(0.30, 0.90, hash21(floor(uv * 420.0)));
    float fogBase = disocclusion * (0.45 + noiseFog * fogNoiseAmount) +
                    (1.0 - position.a) * noiseFog * 0.12 * fogNoiseAmount;
    float fog = clamp(fogBase * max(0.0, fogDensity) / 0.35, 0.0, 1.0);
    vec3 fogColor = mix(vec3(0.025, 0.055, 0.085),
                        vec3(0.20, 0.48, 0.62), noiseFog);
    vec3 color = mix(source.rgb, fogColor, fog);
    // nearby expands point silhouettes; fog/noise hides disocclusion seams.
    float alpha = max(position.a, max(disocclusion * 0.78, fog * 0.45));
    fragColor = TDOutputSwizzle(vec4(color, alpha));
}
''',
    "procedural_backfill": r'''// CONTRACT: POSITION + INTERACTION -> PROCEDURAL_POSITION
out vec4 fragColor;

float hash21(vec2 p)
{
    p = fract(p * vec2(123.34, 345.45));
    p += dot(p, p + 34.345);
    return fract(p.x * p.y);
}

void main()
{
    vec2 uv = vUV.st;
    vec4 measured = texture(sTD2DInputs[0], uv);
    vec4 interaction = texture(sTD2DInputs[1], uv);
    vec2 q = uv * 2.0 - 1.0;
    float radius2 = dot(q, q);
    float shell = sqrt(max(0.0, 1.0 - min(radius2, 1.0)));
    float grain = hash21(floor(uv * 512.0));
    vec3 generated = vec3(q.x * 1.35, -q.y, -1.45 - shell * 0.85);
    generated += (grain - 0.5) * vec3(0.035, 0.035, 0.12);
    generated += interaction.rgb * 0.035;
    float generatedActive = (1.0 - step(1.0, radius2)) * step(0.16, grain);
    float useMeasured = step(0.5, measured.a);
    vec3 position = mix(generated, measured.rgb, useMeasured);
    float activity = max(measured.a, generatedActive * (1.0 - measured.a));
    fragColor = TDOutputSwizzle(vec4(position, activity));
}
''',
    "procedural_color": r'''// CONTRACT: POSITION + PROCEDURAL_POSITION + COLOR -> PROCEDURAL_COLOR
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 originalPosition = texture(sTD2DInputs[0], uv);
    vec4 position = texture(sTD2DInputs[1], uv);
    vec4 source = texture(sTD2DInputs[2], uv);
    float generated = (1.0 - originalPosition.a) * position.a;
    float bands = 0.5 + 0.5 * sin(position.z * 7.0 + position.x * 3.0);
    vec3 palette = mix(vec3(0.055, 0.12, 0.20), vec3(0.78, 0.30, 0.16), bands);
    // Interaction is already folded into PROCEDURAL_POSITION upstream.
    palette += min(length(position.rgb - originalPosition.rgb), 1.0) *
               vec3(0.12, 0.22, 0.32);
    vec3 color = mix(source.rgb, palette, generated);
    fragColor = TDOutputSwizzle(vec4(color, position.a));
}
''',
    "hybrid_completion": r'''// CONTRACT: POSITION + FOG_COLOR + PROCEDURAL_COLOR -> HYBRID_COLOR
out vec4 fragColor;

void main()
{
    const float proceduralMix = 0.72; // FLEXGPU_PROCEDURAL_MIX
    vec2 uv = vUV.st;
    vec4 originalPosition = texture(sTD2DInputs[0], uv);
    vec4 fog = texture(sTD2DInputs[1], uv);
    vec4 procedural = texture(sTD2DInputs[2], uv);
    float hole = 1.0 - originalPosition.a;
    float proceduralWeight = hole * procedural.a * clamp(proceduralMix, 0.0, 1.0);
    vec3 color = mix(fog.rgb, procedural.rgb, proceduralWeight);
    float alpha = max(fog.a, procedural.a);
    fragColor = TDOutputSwizzle(vec4(color, alpha));
}
''',
    "installation_grade": r'''// CONTRACT: POINT_RENDER + FOG_PLATE -> view-aware INSTALLATION
out vec4 fragColor;

float hash21(vec2 p)
{
    p = fract(p * vec2(173.31, 419.17));
    p += dot(p, p + 31.73);
    return fract(p.x * p.y);
}

void main()
{
    const float viewFogDensity = 0.35; // FLEXGPU_VIEW_FOG_DENSITY
    const float viewFogRadius = 2.0; // FLEXGPU_VIEW_FOG_RADIUS
    vec2 uv = vUV.st;
    vec4 points = texture(sTD2DInputs[0], uv);
    vec4 fog = texture(sTD2DInputs[1], uv);
    vec2 texel = 1.0 / vec2(textureSize(sTD2DInputs[0], 0));
    vec2 d = texel * max(1.0, viewFogRadius);
    float neighbours = max(max(texture(sTD2DInputs[0], uv + vec2(d.x, 0.0)).a,
                               texture(sTD2DInputs[0], uv - vec2(d.x, 0.0)).a),
                           max(texture(sTD2DInputs[0], uv + vec2(0.0, d.y)).a,
                               texture(sTD2DInputs[0], uv - vec2(0.0, d.y)).a));
    float edgeHole = neighbours * (1.0 - points.a);
    float grain = 0.35 + 0.65 * hash21(floor(uv * vec2(textureSize(sTD2DInputs[0], 0)) / 3.0));
    float viewFog = clamp(edgeHole * grain * viewFogDensity / 0.35, 0.0, 1.0);
    vec2 p = uv * 2.0 - 1.0;
    float vignette = smoothstep(1.35, 0.24, dot(p, p));
    vec3 edgeColor = mix(vec3(0.018, 0.045, 0.07), fog.rgb, 0.65);
    vec3 color = points.rgb + fog.rgb * fog.a * 0.24 + edgeColor * viewFog;
    color = color / (1.0 + color); // inexpensive tone map
    color *= mix(0.54, 1.0, vignette);
    fragColor = TDOutputSwizzle(vec4(color, 1.0));
}
''',
    "view_completion": r'''// CONTRACT: POINT_RENDER -> view-aware fog/thickness-completed VIEW
out vec4 fragColor;

float hash21(vec2 p)
{
    p = fract(p * vec2(217.13, 391.71));
    p += dot(p, p + 27.19);
    return fract(p.x * p.y);
}

void main()
{
    const float viewFogDensity = 0.35; // FLEXGPU_VIEW_FOG_DENSITY
    const float viewFogRadius = 2.0; // FLEXGPU_VIEW_FOG_RADIUS
    vec2 uv = vUV.st;
    vec4 points = texture(sTD2DInputs[0], uv);
    vec2 texel = 1.0 / vec2(textureSize(sTD2DInputs[0], 0));
    vec2 d = texel * max(1.0, viewFogRadius);
    float neighbours = max(max(texture(sTD2DInputs[0], uv + vec2(d.x, 0.0)).a,
                               texture(sTD2DInputs[0], uv - vec2(d.x, 0.0)).a),
                           max(texture(sTD2DInputs[0], uv + vec2(0.0, d.y)).a,
                               texture(sTD2DInputs[0], uv - vec2(0.0, d.y)).a));
    float edgeHole = neighbours * (1.0 - points.a);
    float grain = 0.35 + 0.65 * hash21(floor(uv * vec2(textureSize(sTD2DInputs[0], 0)) / 3.0));
    float fog = clamp(edgeHole * grain * viewFogDensity / 0.35, 0.0, 1.0);
    vec3 fogColor = mix(vec3(0.012, 0.035, 0.060),
                        vec3(0.10, 0.32, 0.42), grain);
    vec3 color = points.rgb + fogColor * fog;
    color = color / (1.0 + color);
    fragColor = TDOutputSwizzle(vec4(color, 1.0));
}
''',
    "transport_pack_geometry": r'''// CONTRACT: raw DEPTH + CONFIDENCE + MASK -> PACKED_GEOMETRY
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    // Do not clamp/normalize raw depth: metres, millimetres, disparity and
    // inverse depth remain in the calibration-declared encoding.
    float rawDepth = texture(sTD2DInputs[0], uv).r;
    float confidence = clamp(texture(sTD2DInputs[1], uv).r, 0.0, 1.0);
    float mask = clamp(texture(sTD2DInputs[2], uv).r, 0.0, 1.0);
    fragColor = TDOutputSwizzle(vec4(rawDepth, confidence, mask, 1.0));
}
''',
    "transport_pack_atlas": r'''// CONTRACT: RGB + PACKED_GEOMETRY -> atomic RGBA32F ATLAS
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    if (uv.x < 0.5) {
        vec2 sourceUV = vec2(uv.x * 2.0, uv.y);
        vec4 color = texture(sTD2DInputs[0], sourceUV);
        fragColor = TDOutputSwizzle(vec4(color.rgb, 1.0));
    } else {
        vec2 sourceUV = vec2((uv.x - 0.5) * 2.0, uv.y);
        // PACKED_GEOMETRY keeps rawDepth, confidence, mask in RGB so all four
        // image planes still cross the transport boundary in one atlas cook.
        vec4 geometry = texture(sTD2DInputs[1], sourceUV);
        fragColor = TDOutputSwizzle(vec4(geometry.rgb, 1.0));
    }
}
''',
    "transport_unpack_rgb": r'''// CONTRACT: atomic ATLAS -> RGB (left half)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(vUV.st.x * 0.5, vUV.st.y);
    vec4 color = texture(sTD2DInputs[0], sourceUV);
    fragColor = TDOutputSwizzle(vec4(color.rgb, 1.0));
}
''',
    "transport_unpack_depth": r'''// CONTRACT: atomic ATLAS -> raw calibration-encoded DEPTH (right half R)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    float depth = texture(sTD2DInputs[0], sourceUV).r;
    fragColor = TDOutputSwizzle(vec4(depth, depth, depth, 1.0));
}
''',
    "transport_unpack_confidence": r'''// CONTRACT: atomic ATLAS -> CONFIDENCE (right half G)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    float confidence = clamp(texture(sTD2DInputs[0], sourceUV).g, 0.0, 1.0);
    fragColor = TDOutputSwizzle(vec4(confidence, confidence, confidence, 1.0));
}
''',
    "transport_unpack_mask": r'''// CONTRACT: atomic ATLAS -> validity MASK (right half B)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    float mask = clamp(texture(sTD2DInputs[0], sourceUV).b, 0.0, 1.0);
    fragColor = TDOutputSwizzle(vec4(mask, mask, mask, 1.0));
}
''',
    "moge2_unpack_rgb": r'''// CONTRACT: flexgpu-moge2-atlas/v1 -> exact inference RGB (left half)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(vUV.st.x * 0.5, vUV.st.y);
    vec4 color = texture(sTD2DInputs[0], sourceUV);
    fragColor = TDOutputSwizzle(vec4(color.rgb, color.a));
}
''',
    "moge2_unpack_depth": r'''// CONTRACT: flexgpu-moge2-atlas/v1 -> metric optical-Z DEPTH
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    vec4 packed = texture(sTD2DInputs[0], sourceUV);
    vec2 scaleBias = texelFetch(sTD2DInputs[1], ivec2(0, 0), 0).rg;
    float highByte = floor(clamp(packed.r, 0.0, 1.0) * 255.0 + 0.5);
    float lowByte = floor(clamp(packed.g, 0.0, 1.0) * 255.0 + 0.5);
    float uint16Depth = highByte * 256.0 + lowByte;
    float valid = float(uint16Depth > 0.0 && packed.b >= 0.5 && packed.a >= 0.5);
    float metres = (uint16Depth * scaleBias.r + scaleBias.g) * valid;
    fragColor = TDOutputSwizzle(vec4(metres, metres, metres, 1.0));
}
''',
    "moge2_unpack_mask": r'''// CONTRACT: flexgpu-moge2-atlas/v1 -> binary validity MASK (right B)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    float mask = texture(sTD2DInputs[0], sourceUV).b >= 0.5 ? 1.0 : 0.0;
    fragColor = TDOutputSwizzle(vec4(mask, mask, mask, 1.0));
}
''',
    "moge2_unpack_confidence": r'''// CONTRACT: flexgpu-moge2-atlas/v1 -> binary confidence proxy (right A)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    float confidence = texture(sTD2DInputs[0], sourceUV).a >= 0.5 ? 1.0 : 0.0;
    fragColor = TDOutputSwizzle(vec4(confidence, confidence, confidence, 1.0));
}
''',
    "depth_anything_sensor_position": r'''// CONTRACT: packed sensor depth -> sensor-local XYZ metres
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 packed = texture(sTD2DInputs[0], uv);
    vec4 depthCalibration = texelFetch(sTD2DInputs[1], ivec2(0, 0), 0);
    vec4 normalizedIntrinsics = texelFetch(sTD2DInputs[2], ivec2(0, 0), 0);
    float highByte = floor(clamp(packed.r, 0.0, 1.0) * 255.0 + 0.5);
    float lowByte = floor(clamp(packed.g, 0.0, 1.0) * 255.0 + 0.5);
    float uint16Depth = highByte * 256.0 + lowByte;
    float metres = uint16Depth * depthCalibration.r + depthCalibration.g;
    float valid = float(uint16Depth > 0.0 && packed.b >= 0.5 && packed.a > 0.0 &&
                        metres >= depthCalibration.b && metres <= depthCalibration.a);
    vec2 imageSize = vec2(textureSize(sTD2DInputs[0], 0));
    float fx = max(1e-6, normalizedIntrinsics.r * imageSize.x);
    float fy = max(1e-6, normalizedIntrinsics.g * imageSize.y);
    float cx = normalizedIntrinsics.b * imageSize.x;
    float cy = normalizedIntrinsics.a * imageSize.y;
    // The Script TOP flips top-left worker bytes for TD. Convert vUV back to
    // top-left image pixels for pinhole unprojection, then publish the stable
    // FlexGPU camera convention: X right, Y up, Z backward.
    vec2 pixel = vec2(uv.x * imageSize.x, (1.0 - uv.y) * imageSize.y);
    vec3 sensorLocal = vec3((pixel.x - cx) * metres / fx,
                            (cy - pixel.y) * metres / fy,
                            -metres);
    // A is binary occupancy here. OUT_MASK and OUT_CONFIDENCE are multiplied
    // exactly once by the existing SENSOR_VALIDITY stage downstream.
    fragColor = TDOutputSwizzle(vec4(sensorLocal * valid, valid));
}
''',
    "depth_anything_sensor_mask": r'''// CONTRACT: packed sensor B -> binary mask
out vec4 fragColor;

void main()
{
    vec4 packed = texture(sTD2DInputs[0], vUV.st);
    float highByte = floor(clamp(packed.r, 0.0, 1.0) * 255.0 + 0.5);
    float lowByte = floor(clamp(packed.g, 0.0, 1.0) * 255.0 + 0.5);
    float depth = highByte * 256.0 + lowByte;
    float mask = (depth > 0.0 && packed.b >= 0.5 && packed.a > 0.0) ? 1.0 : 0.0;
    fragColor = TDOutputSwizzle(vec4(mask, mask, mask, 1.0));
}
''',
    "depth_anything_sensor_confidence": r'''// CONTRACT: packed sensor A -> confidence
out vec4 fragColor;

void main()
{
    vec4 packed = texture(sTD2DInputs[0], vUV.st);
    float highByte = floor(clamp(packed.r, 0.0, 1.0) * 255.0 + 0.5);
    float lowByte = floor(clamp(packed.g, 0.0, 1.0) * 255.0 + 0.5);
    float depth = highByte * 256.0 + lowByte;
    float valid = (depth > 0.0 && packed.b >= 0.5) ? 1.0 : 0.0;
    float confidence = clamp(packed.a, 0.0, 1.0) * valid;
    fragColor = TDOutputSwizzle(vec4(confidence, confidence, confidence, 1.0));
}
''',
}


MOGE2_SCRIPT_TOP_CALLBACKS = r'''# Script TOP callbacks; OP access stays on TouchDesigner's main thread.
def onSetupParameters(scriptOp):
    return

def onPulse(par):
    return

def onCook(scriptOp):
    module_dat = parent().op('bridge_runtime')
    if module_dat is not None:
        module_dat.module.on_script_top_cook(scriptOp)
    return
'''


MOGE2_EXECUTE_CALLBACKS = r'''# Execute DAT callbacks; the runtime owns only this bridge.
def onStart():
    return

def onCreate():
    return

def onFrameStart(frame):
    module_dat = me.parent().op('bridge_runtime')
    if module_dat is not None:
        module_dat.module.tick(me.parent())
    return

def onExit():
    module_dat = me.parent().op('bridge_runtime')
    if module_dat is not None:
        module_dat.module.stop(me.parent())
    return
'''


DEPTH_ANYTHING_SCRIPT_TOP_CALLBACKS = r'''# Script TOP callback; OP access is main-thread only.
def onSetupParameters(scriptOp):
    return

def onPulse(par):
    return

def onCook(scriptOp):
    module_dat = parent().op('sensor_runtime')
    if module_dat is not None:
        module_dat.module.on_script_top_cook(scriptOp)
    return
'''


DEPTH_ANYTHING_EXECUTE_CALLBACKS = r'''# Execute DAT callbacks for the replaceable sensor bridge.
def onStart():
    return

def onCreate():
    return

def onFrameStart(frame):
    module_dat = me.parent().op('sensor_runtime')
    if module_dat is not None:
        module_dat.module.tick(me.parent())
    return

def onExit():
    module_dat = me.parent().op('sensor_runtime')
    if module_dat is not None:
        module_dat.module.stop(me.parent())
    return
'''


class BuildReport(object):
    """Small report object that is also safe to inspect from the Textport."""

    def __init__(self):
        self.created = []
        self.reused = []
        self.warnings = []

    def warn(self, message):
        message = str(message)
        self.warnings.append(message)
        print("[FlexGPU runtime] WARNING: %s" % message)

    def as_dict(self):
        return {
            "build_version": BUILD_VERSION,
            "created": list(self.created),
            "reused": list(self.reused),
            "warnings": list(self.warnings),
        }


LAST_REPORT = None


def _symbol(name):
    value = globals().get(name)
    if value is not None:
        return value
    try:
        import builtins
        value = getattr(builtins, name, None)
        if value is not None:
            return value
    except Exception:
        pass
    try:
        import td
        return getattr(td, name, None)
    except Exception:
        return None


def _op(path):
    fn = _symbol("op")
    if fn is None:
        raise RuntimeError("TouchDesigner op() is unavailable; run build() inside TouchDesigner 2025.")
    return fn(path)


def _child(parent, name):
    try:
        return parent.op(name)
    except Exception:
        return None


def _operator_type_name(node):
    """Return TouchDesigner's canonical Python operator type when available."""

    # OP.opType is the documented canonical name accepted by COMP.create(),
    # for example ``oscinCHOP`` or ``baseCOMP``. Keep OPType as a compatibility
    # alias for lightweight test/proxy objects used around TouchDesigner.
    for attribute in ("opType", "OPType"):
        try:
            value = getattr(node, attribute)
        except Exception:
            continue
        if value:
            return str(value)
    try:
        value = node.__class__.__name__
    except Exception:
        value = ""
    if any(str(value).lower().endswith(suffix) for suffix in
           ("comp", "top", "chop", "sop", "dat", "mat", "pop")):
        return str(value)
    try:
        operator_type = str(node.type)
        family = str(node.family)
    except Exception:
        return ""
    return operator_type + family


def _operator_type_token(value):
    return "".join(character for character in str(value).lower()
                   if character.isalnum())


def _operator_type_matches(node, expected):
    actual = _operator_type_name(node)
    return bool(actual and
                _operator_type_token(actual) == _operator_type_token(expected))


def _ensure(parent, type_name, name, report, optional=False):
    found = _child(parent, name)
    if found is not None:
        if not _operator_type_matches(found, type_name):
            actual = _operator_type_name(found) or "unverifiable operator type"
            message = "%s already exists as %s; expected %s" % (
                found.path, actual, type_name)
            if optional:
                report.warn(message)
                return None
            raise RuntimeError(message)
        report.reused.append(found.path)
        return found
    errors = []
    for type_value in (type_name, _symbol(type_name)):
        if type_value is None:
            continue
        try:
            node = parent.create(type_value, name)
            report.created.append(node.path)
            return node
        except Exception as exc:
            errors.append(str(exc))
    message = "%s %s unavailable under %s" % (type_name, name, parent.path)
    if errors:
        message += " (%s)" % errors[-1]
    if optional:
        report.warn(message)
        return None
    raise RuntimeError(message)


def _par(node, *names):
    if node is None:
        return None
    for name in names:
        try:
            value = getattr(node.par, name)
            if value is not None:
                return value
        except Exception:
            pass
    # TouchDesigner canonicalizes multi-word custom names (for example,
    # ``UseStreamDiffusion`` becomes ``Usestreamdiffusion``).  Resolve those
    # parameters case-insensitively so an idempotent rebuild finds the original
    # parameter instead of attempting to append a duplicate.
    wanted = {str(name).lower() for name in names}
    try:
        for parameter in node.pars():
            if str(parameter.name).lower() in wanted:
                return parameter
    except Exception:
        pass
    return None


def _set(node, names, value):
    if isinstance(names, str):
        names = (names,)
    parameter = _par(node, *names)
    if parameter is None:
        return False
    try:
        parameter.val = value
        return True
    except Exception:
        return False


def _expr(node, names, expression):
    if isinstance(names, str):
        names = (names,)
    parameter = _par(node, *names)
    if parameter is None:
        return False
    try:
        parameter.expr = expression
        return True
    except Exception:
        return False


def _set_sequence_blocks(node, name, minimum):
    """Ensure a built-in sequential parameter has at least ``minimum`` blocks."""

    if node is None:
        return False
    try:
        sequence = getattr(node.seq, name)
        if sequence.numBlocks < minimum:
            sequence.numBlocks = minimum
        return sequence.numBlocks >= minimum
    except Exception:
        return False


def _style(node, x, y, color, comment, width=180, height=90):
    for attr, value in (("nodeX", x), ("nodeY", y), ("color", color),
                        ("comment", comment), ("nodeWidth", width),
                        ("nodeHeight", height)):
        try:
            setattr(node, attr, value)
        except Exception:
            pass


def _connect(src, dst, dst_index=0, src_index=0, report=None, replace=False):
    if src is None or dst is None:
        return False
    if not replace:
        try:
            inputs = dst.inputs
            if len(inputs) > dst_index and inputs[dst_index] is not None:
                return True
        except Exception:
            pass
    try:
        dst.setInput(dst_index, src, src_index)
        return True
    except Exception:
        pass
    try:
        dst.inputConnectors[dst_index].connect(src.outputConnectors[src_index])
        return True
    except Exception as exc:
        if report is not None:
            report.warn("Could not connect %s[%s] -> %s[%s]: %s" %
                        (src.path, src_index, dst.path, dst_index, exc))
        return False


def _disconnect_input(dst, dst_index, report=None):
    """Clear one managed input while tolerating TouchDesigner API variants."""
    if dst is None:
        return False
    try:
        dst.setInput(dst_index, None)
        return True
    except Exception:
        pass
    try:
        dst.inputConnectors[dst_index].disconnect()
        return True
    except Exception as exc:
        if report is not None:
            report.warn("Could not clear %s input %s: %s" %
                        (dst.path, dst_index, exc))
        return False


def _text(parent, name, body, report):
    dat = _ensure(parent, "textDAT", name, report)
    try:
        dat.text = body
    except Exception as exc:
        report.warn("Could not update %s: %s" % (dat.path, exc))
    return dat


def _table(parent, name, rows, report):
    dat = _ensure(parent, "tableDAT", name, report)
    try:
        dat.clear()
        for row in rows:
            dat.appendRow([str(value) for value in row])
    except Exception as exc:
        report.warn("Could not update %s: %s" % (dat.path, exc))
    return dat


def _page(comp, name):
    try:
        for page in comp.customPages:
            if page.name == name:
                return page
    except Exception:
        pass
    try:
        return comp.appendCustomPage(name)
    except Exception:
        return None


def _custom(comp, page, kind, name, default, menu=None, label=None):
    existing = _par(comp, name)
    if existing is not None:
        if menu and kind == "Menu":
            try:
                existing.menuNames = list(menu)
                existing.menuLabels = [
                    str(value).replace("_", " ").title() for value in menu]
            except Exception:
                pass
        return existing
    if page is None:
        return None
    method = getattr(page, "append%s" % kind, None)
    if method is None:
        return None
    try:
        # TouchDesigner custom parameter identifiers must be capitalized once;
        # embedded capitals are rejected rather than normalized by append*().
        # Keep the readable label while creating the canonical identifier.
        canonical_name = str(name)[:1].upper() + str(name)[1:].lower()
        result = method(canonical_name, label=label or name)
        parameter = result[0] if isinstance(result, (list, tuple)) else result
        if menu and kind == "Menu":
            parameter.menuNames = list(menu)
            parameter.menuLabels = [str(value).replace("_", " ").title() for value in menu]
        parameter.default = default
        parameter.val = default
        return parameter
    except Exception:
        return None


def _in_top(parent, name, index, report):
    node = _ensure(parent, "inTOP", name, report)
    # In/Out TOP connectors are ordered by Connect Order (`connectorder`) in
    # TouchDesigner 2025.  inputindex/outputindex are not In/Out TOP params;
    # without this every connector silently falls back to name ordering.
    _set(node, ("connectorder", "inputindex", "index"), index)
    return node


def _out_top(parent, name, source, index, report):
    node = _ensure(parent, "outTOP", name, report)
    _set(node, ("connectorder", "outputindex", "index"), index)
    # Out TOPs are part of the managed component contract.  Replacing their
    # source is required when an older project is rebuilt after a stage rename.
    _connect(source, node, report=report, replace=True)
    return node


def _glsl(parent, name, shader_name, inputs, report, float_output=False):
    # A GLSL TOP in the supported TouchDesigner 2025 build exposes three
    # wired inputs. Keep this guard beside node creation so a future shader
    # cannot silently build an operator that compiles only as a GLSL Multi TOP.
    if len(inputs) > 3:
        raise ValueError("GLSL TOP %s exceeds the three-input limit" % name)
    source = _text(parent, "%s_PIXEL" % name, SHADERS[shader_name], report)
    node = _ensure(parent, "glslTOP", name, report)
    _set(node, ("pixeldat", "pixelshader"), source.path)
    _set(node, "outputresolution", "useinput")
    if float_output:
        _set(node, "format", "rgba32float")
    else:
        _set(node, "format", "rgba16float")
    # This managed shader owns its complete input map. Replacing declared
    # inputs and clearing surplus connectors is required for idempotent
    # upgrades from earlier builds whose shaders used four or five inputs.
    for index, input_node in enumerate(inputs):
        _connect(input_node, node, index, 0, report, replace=True)
    try:
        connector_count = len(node.inputConnectors)
    except Exception:
        try:
            connector_count = len(node.inputs)
        except Exception:
            connector_count = len(inputs)
    for index in range(len(inputs), connector_count):
        _disconnect_input(node, index, report)
    return node


def _set_resolution(node, width, height):
    _set(node, "outputresolution", "custom")
    # Keep explicit geometry/output budgets deterministic even when the host
    # project has TouchDesigner's global resolution multiplier enabled.
    _set(node, "resmult", False)
    _set(node, ("resolutionw", "resw"), width)
    _set(node, ("resolutionh", "resh"), height)


def _moge2_runtime_source(report):
    """Load the import-safe bridge module that will be embedded in a Text DAT."""

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "moge2_bridge_runtime.py")
    try:
        size = os.path.getsize(path)
        if size < 1 or size > 512 * 1024:
            raise ValueError("bridge runtime source is outside the size limit")
        with open(path, "r", encoding="utf-8") as stream:
            return stream.read()
    except Exception as exc:
        report.warn("MoGe-2 bridge runtime could not be embedded: %s" % exc)
        return ("def tick(bridge_comp=None):\n    return None\n\n"
                "def stop(bridge_comp=None):\n    return None\n\n"
                "def on_script_top_cook(script_op):\n    return None\n")


def _build_moge2_bridge(adapter, input_rgb, report):
    """Build a default-off, external-worker bridge without loading PyTorch in TD."""

    comp = _ensure(adapter, "baseCOMP", "MOGE2_BRIDGE", report)
    _style(comp, 360, 180, (0.20, 0.39, 0.56),
           "Opt-in MoGe-2 worker: newest RGB -> synchronized RGB/metric depth",
           310, 135)
    page = _page(comp, "MoGe-2 Bridge")
    _custom(comp, page, "Toggle", "Enabled", False)
    _custom(comp, page, "Menu", "Profile", "3080ti_16gb",
            ("3080ti_16gb", "4090", "5090"))
    _custom(comp, page, "Str", "Workerhost", "127.0.0.1",
            label="Worker Host")
    _custom(comp, page, "Int", "Workerinputtcp", 9211,
            label="Worker Input TCP")
    _custom(comp, page, "Int", "Workerinputudp", 9210,
            label="Worker Input UDP")
    _custom(comp, page, "Str", "Resultbindhost", "127.0.0.1",
            label="Result Bind Host")
    _custom(comp, page, "Int", "Resulttcp", 9221,
            label="Result TCP")
    _custom(comp, page, "Int", "Resultudp", 9220,
            label="Result UDP")
    _custom(comp, page, "Int", "Capturefps", 5,
            label="Geometry Capture FPS")
    _custom(comp, page, "Toggle", "Flipvertical", True,
            label="TD / Image Vertical Flip")
    _custom(comp, page, "Toggle", "Resultvalid", False,
            label="Synchronized Result Valid")
    _custom(comp, page, "Str", "Generationid", "streamdiffusion",
            label="Prompt Generation ID")
    _custom(comp, page, "Int", "Sourceframeid", 0,
            label="Source Frame ID")

    rgb_in = _in_top(comp, "IN_RGB", 0, report)
    _connect(input_rgb, comp, 0, 0, report, replace=False)

    runtime_dat = _text(comp, "bridge_runtime", _moge2_runtime_source(report), report)
    script_callbacks = _text(
        comp, "script_top_callbacks", MOGE2_SCRIPT_TOP_CALLBACKS, report)
    atlas = _ensure(comp, "scriptTOP", "RESULT_ATLAS", report)
    _set(atlas, ("callbacks", "callbacksdat"), script_callbacks.path)
    # Script TOPs in TouchDesigner 2025.32820 do not expose ``alwayscook``.
    # The Execute DAT stages each immutable result and calls cook(force=True);
    # its callback must confirm the exact uploaded key before routes go valid.
    _set_resolution(atlas, 2, 1)
    _set(atlas, "format", "rgba8fixed")

    execute = _ensure(comp, "executeDAT", "bridge_callbacks", report,
                      optional=True)
    if execute is not None:
        try:
            execute.text = MOGE2_EXECUTE_CALLBACKS
        except Exception:
            pass
        _set(execute, ("start", "onstart"), True)
        _set(execute, ("create", "oncreate"), True)
        _set(execute, ("framestart", "onframestart"), True)
        _set(execute, ("exit", "onexit"), True)
        _set(execute, "active", True)
    else:
        _text(comp, "bridge_callbacks_SOURCE", MOGE2_EXECUTE_CALLBACKS, report)

    scale_bias = _ensure(comp, "constantTOP", "DEPTH_SCALE_BIAS", report)
    _set_resolution(scale_bias, 1, 1)
    _set(scale_bias, "format", "rgba32float")
    _set(scale_bias, ("colorr", "color1r"), 0.001)
    _set(scale_bias, ("colorg", "color1g"), 0.0)
    _set(scale_bias, ("colorb", "color1b"), 0.0)
    _set(scale_bias, ("colora", "color1a", "alpha"), 1.0)

    rgb = _glsl(comp, "UNPACK_RGB", "moge2_unpack_rgb", [atlas], report)
    depth = _glsl(comp, "UNPACK_DEPTH_METRES", "moge2_unpack_depth",
                  [atlas, scale_bias], report, True)
    mask = _glsl(comp, "UNPACK_MASK", "moge2_unpack_mask", [atlas], report)
    confidence = _glsl(comp, "UNPACK_CONFIDENCE",
                       "moge2_unpack_confidence", [atlas], report)
    for node in (rgb, depth, mask, confidence):
        _set(node, "outputresolution", "custom")
        _set(node, "resmult", False)
        _expr(node, ("resolutionw", "resw"),
              "max(1, int(op('RESULT_ATLAS').width) // 2)")
        _expr(node, ("resolutionh", "resh"),
              "max(1, int(op('RESULT_ATLAS').height))")
    _set(rgb, "format", "rgba16float")
    _set(depth, "format", "mono32float")
    _set(mask, "format", "mono8fixed")
    _set(confidence, "format", "mono8fixed")

    _out_top(comp, "OUT_RGB", rgb, 0, report)
    _out_top(comp, "OUT_DEPTH", depth, 1, report)
    _out_top(comp, "OUT_CONFIDENCE", confidence, 2, report)
    _out_top(comp, "OUT_MASK", mask, 3, report)
    _text(comp, "FRAME_STATE", "{}\n", report)
    _text(comp, "CAMERA_METADATA", "{}\n", report)
    _table(comp, "STATUS", [["metric", "value"],
                             ["state", "disabled"],
                             ["detail", "enable only after the worker is listening"]],
           report)
    _text(comp, "README_FIRST",
          "MOGE-2 LIVE BRIDGE (DEFAULT OFF)\n\n"
          "IN_RGB must be the exact StreamDiffusionTD image. The external worker "
          "owns PyTorch/MoGe and returns a WorldBus rgba8_atlas. This COMP publishes "
          "the returned RGB with its metric depth/mask/confidence so frames cannot "
          "cross. TD operator access remains on the main thread; socket threads move "
          "bounded immutable bytes only. FRAME_STATE and CAMERA_METADATA describe "
          "the same confirmed atlas upload. Resultvalid stays false until a forced "
          "Script TOP cook copies the exact staged key. Do not put secrets or prompt "
          "text in metadata.",
          report)
    try:
        comp.store("moge2_bridge_runtime_dat", runtime_dat.path)
    except Exception:
        pass
    return comp


def _wire_moge2_routes(adapter, moge2, fallbacks, report):
    """Route a complete synchronized result only after the bridge marks it valid."""

    if len(fallbacks) != 4 or any(node is None for node in fallbacks):
        raise RuntimeError("MoGe-2 route requires existing RGB/depth/confidence/mask fallbacks")
    route_specs = (
        ("MOGE2_RGB_ROUTE", fallbacks[0], 0),
        ("MOGE2_DEPTH_ROUTE", fallbacks[1], 1),
        ("MOGE2_CONFIDENCE_ROUTE", fallbacks[2], 2),
        ("MOGE2_MASK_ROUTE", fallbacks[3], 3),
    )
    routes = []
    for name, fallback, output_index in route_specs:
        route = _ensure(adapter, "switchTOP", name, report)
        _connect(fallback, route, 0, 0, report, replace=True)
        _connect(moge2, route, 1, output_index, report, replace=True)
        _expr(route, "index",
              "1 if (op('MOGE2_BRIDGE').par.Enabled and "
              "op('MOGE2_BRIDGE').par.Resultvalid) else 0")
        routes.append(route)
    for index, (name, route) in enumerate(zip(
        ("OUT_RGB", "OUT_DEPTH", "OUT_CONFIDENCE", "OUT_MASK"), routes)):
        _out_top(adapter, name, route, index, report)
    return tuple(routes)


def _depth_anything_runtime_source(report):
    """Load the import-safe sensor receiver for embedding in a Text DAT."""

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "depth_anything_sensor_runtime.py")
    try:
        size = os.path.getsize(path)
        if size < 1 or size > 512 * 1024:
            raise ValueError("sensor runtime source is outside the size limit")
        with open(path, "r", encoding="utf-8") as stream:
            return stream.read()
    except Exception as exc:
        report.warn("Depth Anything sensor runtime could not be embedded: %s" % exc)
        return ("def tick(bridge_comp=None):\n    return None\n\n"
                "def stop(bridge_comp=None):\n    return None\n\n"
                "def on_script_top_cook(script_op):\n    return None\n")


def _build_depth_anything_sensor_bridge(adapter, report):
    """Build a backend-replaceable, result-only audience sensor receiver."""

    comp = _ensure(adapter, "baseCOMP", "DEPTH_ANYTHING_BRIDGE", report)
    _style(comp, 390, 120, (0.20, 0.48, 0.42),
           "Replaceable no-RGB sensor bridge: packed depth/mask/confidence",
           320, 140)
    page = _page(comp, "Depth Anything Sensor")
    _custom(comp, page, "Toggle", "Enabled", False,
            label="Follow Adapter Enabled")
    # DEPTH_SENSOR_ADAPTER.Enabled is the existing stable control surface used
    # by runtime_helpers. The nested bridge remains default-off and follows it.
    _expr(comp, "Enabled", "parent().par.Enabled")
    _custom(comp, page, "Str", "Resultbindhost", "127.0.0.1",
            label="Result Bind Host")
    _custom(comp, page, "Int", "Resulttcp", 9241, label="Result TCP")
    _custom(comp, page, "Int", "Resultudp", 9240,
            label="Reserved UDP (unused)")
    _custom(comp, page, "Toggle", "Allowtrustednetwork", False,
            label="Allow Trusted Network Bind")
    _custom(comp, page, "Float", "Stalems", 800.0,
            label="Capture Freshness (ms)")
    _custom(comp, page, "Toggle", "Flipvertical", True,
            label="Worker / TD Vertical Flip")
    _custom(comp, page, "Toggle", "Resultvalid", False,
            label="Fresh Correlated Result")

    runtime_dat = _text(
        comp, "sensor_runtime", _depth_anything_runtime_source(report), report)
    script_callbacks = _text(
        comp, "script_top_callbacks", DEPTH_ANYTHING_SCRIPT_TOP_CALLBACKS, report)
    packed = _ensure(comp, "scriptTOP", "RESULT_PACKED", report)
    _set(packed, ("callbacks", "callbacksdat"), script_callbacks.path)
    # TD 2025 Script TOP has no alwayscook. Execute DAT stages immutable bytes,
    # force-cooks this TOP, and requires the callback to confirm the exact key.
    _set_resolution(packed, 256, 144)
    _set(packed, "format", "rgba8fixed")

    execute = _ensure(comp, "executeDAT", "sensor_callbacks", report,
                      optional=True)
    if execute is not None:
        try:
            execute.text = DEPTH_ANYTHING_EXECUTE_CALLBACKS
        except Exception:
            pass
        _set(execute, ("start", "onstart"), True)
        _set(execute, ("create", "oncreate"), True)
        _set(execute, ("framestart", "onframestart"), True)
        _set(execute, ("exit", "onexit"), True)
        _set(execute, "active", True)
    else:
        _text(comp, "sensor_callbacks_SOURCE",
              DEPTH_ANYTHING_EXECUTE_CALLBACKS, report)

    depth_calibration = _ensure(comp, "constantTOP", "DEPTH_CALIBRATION", report)
    _set_resolution(depth_calibration, 1, 1)
    _set(depth_calibration, "format", "rgba32float")
    for names, value in (
        (("colorr", "color1r"), 0.001),
        (("colorg", "color1g"), 0.0),
        (("colorb", "color1b"), 0.5),
        (("colora", "color1a", "alpha"), 4.0),
    ):
        _set(depth_calibration, names, value)
    intrinsics = _ensure(comp, "constantTOP", "INTRINSICS_NORMALIZED", report)
    _set_resolution(intrinsics, 1, 1)
    _set(intrinsics, "format", "rgba32float")
    for names, value in (
        (("colorr", "color1r"), 0.8660254),
        (("colorg", "color1g"), 1.5396007),
        (("colorb", "color1b"), 0.5),
        (("colora", "color1a", "alpha"), 0.5),
    ):
        _set(intrinsics, names, value)

    position = _glsl(
        comp, "UNPACK_SENSOR_POSITION", "depth_anything_sensor_position",
        [packed, depth_calibration, intrinsics], report, True)
    mask = _glsl(comp, "UNPACK_SENSOR_MASK", "depth_anything_sensor_mask",
                 [packed], report)
    confidence = _glsl(
        comp, "UNPACK_SENSOR_CONFIDENCE", "depth_anything_sensor_confidence",
        [packed], report)
    for node in (position, mask, confidence):
        _set(node, "outputresolution", "custom")
        _set(node, "resmult", False)
        _expr(node, ("resolutionw", "resw"),
              "max(1, int(op('RESULT_PACKED').width))")
        _expr(node, ("resolutionh", "resh"),
              "max(1, int(op('RESULT_PACKED').height))")
    _set(position, "format", "rgba32float")
    _set(mask, "format", "mono8fixed")
    _set(confidence, "format", "mono8fixed")

    _out_top(comp, "OUT_POSITION", position, 0, report)
    _out_top(comp, "OUT_MASK", mask, 1, report)
    _out_top(comp, "OUT_CONFIDENCE", confidence, 2, report)
    _text(comp, "FRAME_STATE", "{}\n", report)
    _table(comp, "STATUS", [
        ["metric", "value"],
        ["state", "disabled"],
        ["detail", "enable adapter, then start an external sensor producer"],
    ], report)
    _text(comp, "README_FIRST",
          "REPLACEABLE AUDIENCE SENSOR BRIDGE (DEFAULT OFF)\n\n"
          "The temporary worker receives the laptop webcam locally and sends "
          "only uint16 pseudo-metre depth, mask, confidence, and bounded "
          "metadata. No RGB enters this COMP. A paid Depth Anything app or a "
          "future hardware depth sensor may replace that worker by mapping "
          "Spout, NDI, TOP, or API output to the same OUT_POSITION, OUT_MASK, "
          "OUT_CONFIDENCE, and FRAME_STATE contracts. OUT_POSITION is strictly "
          "sensor-local XYZ; the parent CALIBRATE_SENSOR_POSITION applies the "
          "independent sensor_to_world transform. Socket threads retain bytes "
          "only. Stale, malformed, disconnected, or calibration-changing input "
          "sets Resultvalid false; enabled routes then publish zero occupancy. "
          "TCP binds to loopback unless Allow Trusted Network Bind is explicitly "
          "enabled; reserved UDP 9240 is metadata only and is not opened.",
          report)
    try:
        comp.store("depth_anything_sensor_runtime_dat", runtime_dat.path)
    except Exception:
        pass
    return comp


def _wire_depth_anything_sensor_routes(adapter, bridge, fallbacks, report):
    """Preserve disabled fallbacks but fail closed while the bridge is enabled."""

    if len(fallbacks) != 3 or any(node is None for node in fallbacks):
        raise RuntimeError("Depth Anything routes require three adapter fallbacks")
    zero = _ensure(adapter, "constantTOP", "DEPTH_ANYTHING_FAIL_CLOSED_ZERO", report)
    _set_resolution(zero, 256, 144)
    _set(zero, "format", "rgba32float")
    for names in (("colorr", "color1r"), ("colorg", "color1g"),
                  ("colorb", "color1b"), ("colora", "color1a", "alpha")):
        _set(zero, names, 0.0)
    route_specs = (
        ("DEPTH_ANYTHING_POSITION_ROUTE", fallbacks[0], 0),
        ("DEPTH_ANYTHING_MASK_ROUTE", fallbacks[1], 1),
        ("DEPTH_ANYTHING_CONFIDENCE_ROUTE", fallbacks[2], 2),
    )
    routes = []
    for name, fallback, output_index in route_specs:
        route = _ensure(adapter, "switchTOP", name, report)
        _connect(fallback, route, 0, 0, report, replace=True)
        _connect(zero, route, 1, 0, report, replace=True)
        _connect(bridge, route, 2, output_index, report, replace=True)
        _expr(route, "index",
              "2 if (op('DEPTH_ANYTHING_BRIDGE').par.Enabled and "
              "op('DEPTH_ANYTHING_BRIDGE').par.Resultvalid) else "
              "(1 if op('DEPTH_ANYTHING_BRIDGE').par.Enabled else 0)")
        routes.append(route)
    for index, (name, route) in enumerate(zip(
        ("OUT_POSITION", "OUT_MASK", "OUT_CONFIDENCE"), routes)):
        _out_top(adapter, name, route, index, report)
    return tuple(routes)


def _build_sources(parent, report):
    comp = _ensure(parent, "baseCOMP", "SOURCES", report)
    _style(comp, -1320, 300, (0.42, 0.22, 0.54),
           "Demo now; drop StreamDiffusionTD.tox into its explicit adapter", 250, 115)

    page = _page(comp, "Source")
    use_stream = _custom(comp, page, "Toggle", "UseStreamDiffusion", False,
                         label="Use StreamDiffusion Adapter")
    use_depth = _custom(comp, page, "Toggle", "UseExternalDepth", False,
                        label="Use Adapter Depth")
    _custom(comp, page, "Int", "Frameid", -1, label="Source Frame ID")
    _custom(comp, page, "Int", "Sessionepoch", 0,
            label="Source Session / Generation Epoch")
    _custom(comp, page, "Float", "Sourceagems", -1.0,
            label="Source Age (ms; -1 unknown)")
    _custom(comp, page, "Toggle", "Newframe", True,
            label="Accepted New Frame (one cook pulse)")
    _custom(comp, page, "Toggle", "Sourcevalid", True,
            label="Source Frame Valid / Fresh")
    _custom(comp, page, "Float", "Frametimestampseconds", -1.0,
            label="Source Timestamp (seconds; -1 unknown)")

    demo_rgb = _ensure(comp, "noiseTOP", "DEMO_RGB_GENERATOR", report)
    _set_resolution(demo_rgb, 512, 512)
    _set(demo_rgb, ("type", "noisetype"), "sparse")
    _set(demo_rgb, ("period", "periodx"), 3.0)
    _expr(demo_rgb, ("translatex", "tx"), "absTime.seconds * 0.08")
    _expr(demo_rgb, ("translatey", "ty"), "absTime.seconds * -0.045")

    demo_depth = _ensure(comp, "noiseTOP", "DEMO_DEPTH_GENERATOR", report)
    _set_resolution(demo_depth, 384, 384)
    _set(demo_depth, ("monochrome", "mono"), True)
    _set(demo_depth, ("period", "periodx"), 1.7)
    _expr(demo_depth, ("translatez", "tz"), "absTime.seconds * 0.10")

    adapter = _ensure(comp, "baseCOMP", "STREAMDIFFUSION_ADAPTER", report)
    _style(adapter, -120, 160, (0.60, 0.19, 0.40),
           "REPLACE THESE TWO TOPs WITH StreamDiffusionTD.tox OUTPUTS", 300, 130)
    adapter_page = _page(adapter, "Adapter")
    _custom(adapter, adapter_page, "Toggle", "Enabled", False)
    _custom(adapter, adapter_page, "Str", "RGBContract", TOP_CONTRACTS["RGB"])
    _custom(adapter, adapter_page, "Str", "DepthContract", TOP_CONTRACTS["DEPTH"])
    tox_rgb = _ensure(adapter, "constantTOP", "REPLACE_WITH_STREAMDIFFUSION_RGB", report)
    _set_resolution(tox_rgb, 512, 512)
    _set(tox_rgb, ("colorr", "color1r"), 0.04)
    _set(tox_rgb, ("colorg", "color1g"), 0.01)
    _set(tox_rgb, ("colorb", "color1b"), 0.06)
    tox_depth = _ensure(adapter, "constantTOP", "REPLACE_WITH_DEPTH_ESTIMATE", report)
    _set_resolution(tox_depth, 384, 384)
    _set(tox_depth, ("colorr", "color1r"), 0.45)
    _set(tox_depth, ("colorg", "color1g"), 0.45)
    _set(tox_depth, ("colorb", "color1b"), 0.45)
    tox_confidence = _ensure(adapter, "constantTOP", "REPLACE_WITH_CONFIDENCE", report)
    _set_resolution(tox_confidence, 384, 384)
    _set(tox_confidence, ("colorr", "color1r"), 1.0)
    _set(tox_confidence, ("colorg", "color1g"), 1.0)
    _set(tox_confidence, ("colorb", "color1b"), 1.0)
    tox_mask = _ensure(adapter, "constantTOP", "REPLACE_WITH_VALID_MASK", report)
    _set_resolution(tox_mask, 384, 384)
    _set(tox_mask, ("colorr", "color1r"), 1.0)
    _set(tox_mask, ("colorg", "color1g"), 1.0)
    _set(tox_mask, ("colorb", "color1b"), 1.0)
    moge2 = _build_moge2_bridge(adapter, tox_rgb, report)
    _wire_moge2_routes(
        adapter, moge2, (tox_rgb, tox_depth, tox_confidence, tox_mask), report)
    _table(adapter, "ADAPTER_CONTRACT", [
        ["output", "required contract", "replace node"],
        ["OUT_RGB", TOP_CONTRACTS["RGB"], "MOGE2_RGB_ROUTE"],
        ["OUT_DEPTH", TOP_CONTRACTS["DEPTH"], "MOGE2_DEPTH_ROUTE"],
        ["OUT_CONFIDENCE", TOP_CONTRACTS["CONFIDENCE"], "MOGE2_CONFIDENCE_ROUTE"],
        ["OUT_MASK", "R valid mask normalized 0..1", "MOGE2_MASK_ROUTE"],
    ], report)
    _text(adapter, "README_FIRST", "STREAMDIFFUSIONTD ADAPTER BOUNDARY\n\n"
          "Demo mode works without this branch. Later place StreamDiffusionTD.tox here, "
          "wire its image to OUT_RGB, depth estimate to OUT_DEPTH, and optional "
          "validity/confidence to OUT_CONFIDENCE. Increment Session Epoch when a "
          "model, prompt generation, calibration, or producer session changes. "
          "If the TOX emits only RGB, keep DEMO_DEPTH or replace OUT_DEPTH with any depth model. "
          "Do not modify downstream POSITION/COLOR contracts.", report)

    rgb_switch = _ensure(comp, "switchTOP", "RGB_SOURCE", report)
    depth_switch = _ensure(comp, "switchTOP", "DEPTH_SOURCE", report)
    demo_confidence = _ensure(comp, "constantTOP", "DEMO_CONFIDENCE", report)
    _set_resolution(demo_confidence, 384, 384)
    _set(demo_confidence, ("colorr", "color1r"), 1.0)
    _set(demo_confidence, ("colorg", "color1g"), 1.0)
    _set(demo_confidence, ("colorb", "color1b"), 1.0)
    demo_mask = _ensure(comp, "constantTOP", "DEMO_VALID_MASK", report)
    _set_resolution(demo_mask, 384, 384)
    _set(demo_mask, ("colorr", "color1r"), 1.0)
    _set(demo_mask, ("colorg", "color1g"), 1.0)
    _set(demo_mask, ("colorb", "color1b"), 1.0)
    confidence_switch = _ensure(comp, "switchTOP", "CONFIDENCE_SOURCE", report)
    mask_switch = _ensure(comp, "switchTOP", "VALID_MASK_SOURCE", report)
    _connect(demo_rgb, rgb_switch, 0, 0, report, replace=True)
    _connect(adapter, rgb_switch, 1, 0, report, replace=True)
    _connect(demo_depth, depth_switch, 0, 0, report, replace=True)
    _connect(adapter, depth_switch, 1, 1, report, replace=True)
    _connect(demo_confidence, confidence_switch, 0, 0, report, replace=True)
    _connect(adapter, confidence_switch, 1, 2, report, replace=True)
    _connect(demo_mask, mask_switch, 0, 0, report, replace=True)
    _connect(adapter, mask_switch, 1, 3, report, replace=True)
    stream_name = use_stream.name if use_stream is not None else "Usestreamdiffusion"
    depth_name = use_depth.name if use_depth is not None else "Useexternaldepth"
    _expr(rgb_switch, "index", "1 if parent().par.%s else 0" % stream_name)
    _expr(depth_switch, "index", "1 if parent().par.%s else 0" % depth_name)
    _expr(confidence_switch, "index", "1 if parent().par.%s else 0" % depth_name)
    _expr(mask_switch, "index", "1 if parent().par.%s else 0" % depth_name)
    validity = _glsl(comp, "COMBINE_VALIDITY", "validity_combine",
                     [mask_switch, confidence_switch], report, False)
    _out_top(comp, "OUT_RGB", rgb_switch, 0, report)
    _out_top(comp, "OUT_DEPTH", depth_switch, 1, report)
    # Keep raw confidence and mask independently transportable. The bridge
    # combines them after either the local or remote route is selected.
    _out_top(comp, "OUT_CONFIDENCE", confidence_switch, 2, report)
    _out_top(comp, "OUT_MASK", mask_switch, 3, report)
    _out_top(comp, "OUT_VALIDITY", validity, 4, report)
    _table(comp, "SOURCE_STATUS", [
        ["mode", "RGB", "depth"],
        ["default", "DEMO_RGB_GENERATOR", "DEMO_DEPTH_GENERATOR"],
        ["future", "STREAMDIFFUSION_ADAPTER/OUT_RGB", "STREAMDIFFUSION_ADAPTER/OUT_DEPTH + OUT_CONFIDENCE"],
    ], report)
    return comp


def _build_role_bridge(parent, report):
    """Build an atomic RGB/depth/validity bridge for split process roles.

    The sender packs RGB and raw depth/confidence/mask into one RGBA32F TOP before it
    crosses process/machine boundaries, so a receiver cannot combine textures
    from different generation frames. ``local`` bypasses pack/unpack entirely.
    This is a direct image bridge, not the richer WorldBus v1 metadata/control
    protocol implemented by ``src/flexgpu/worldbus.py``.
    """
    comp = _ensure(parent, "baseCOMP", "ROLE_BRIDGE", report)
    _style(comp, -1160, 80, (0.18, 0.38, 0.58),
           "Atomic RGB/raw-depth/confidence/mask atlas plus frame-state boundary",
           285, 125)
    page = _page(comp, "Role Bridge")
    _custom(comp, page, "Menu", "Mode", "local",
            ("local", "send_shared", "receive_shared", "send_tcp", "receive_tcp"))
    _custom(comp, page, "Toggle", "Senderactive", False,
            label="Sender Active")
    _custom(comp, page, "Toggle", "Receiveractive", False,
            label="Receiver Active")
    _custom(comp, page, "Str", "Segmentname", "FlexShowWorldBus",
            label="Shared Memory Segment")
    _custom(comp, page, "Str", "Peeraddress", "127.0.0.1",
            label="Touch In Peer Address")
    _custom(comp, page, "Int", "Atlaswidth", 1024, label="Atlas Width")
    _custom(comp, page, "Int", "Atlasheight", 512, label="Atlas Height")
    _custom(comp, page, "Int", "Atlasport", 12000, label="Atlas Port")
    _custom(comp, page, "Int", "Sendfps", 5, label="Transport FPS")
    _custom(comp, page, "Int", "Sendstep", 12, label="Touch Send Step")
    _custom(comp, page, "Str", "Framesessionid", "legacy-local",
            label="Accepted Frame Session ID")
    _custom(comp, page, "Int", "Frameid", -1,
            label="Accepted Frame ID")
    _custom(comp, page, "Str", "Frametimestampns", "-1",
            label="Accepted Frame Timestamp (ns)")
    _custom(comp, page, "Str", "Calibrationid", "",
            label="Calibration ID")
    _custom(comp, page, "Str", "Calibrationdigest", "",
            label="Calibration Content SHA-256")
    _custom(comp, page, "Toggle", "Framevalid", True,
            label="Accepted Frame Fresh / Valid")

    def required(node, names, value, expression=False):
        """Set a documented endpoint parameter or surface API drift loudly."""
        if node is None:
            return False
        setter = _expr if expression else _set
        if setter(node, names, value):
            return True
        shown = names if isinstance(names, str) else "/".join(names)
        report.warn("%s is missing required transport parameter %s" %
                    (node.path, shown))
        return False

    local_rgb = _in_top(comp, "LOCAL_RGB", 0, report)
    local_depth = _in_top(comp, "LOCAL_DEPTH", 1, report)
    local_confidence = _in_top(comp, "LOCAL_CONFIDENCE", 2, report)
    local_mask = _in_top(comp, "LOCAL_MASK", 3, report)

    packed_geometry = _glsl(
        comp, "PACK_DEPTH_PLANES", "transport_pack_geometry",
        [local_depth, local_confidence, local_mask], report, True)
    atlas_pack = _glsl(comp, "PACK_ATOMIC_ATLAS", "transport_pack_atlas",
                       [local_rgb, packed_geometry], report, True)
    _set(atlas_pack, "outputresolution", "custom")
    _set(atlas_pack, "resmult", False)
    _expr(atlas_pack, ("resolutionw", "resw"),
          "max(2, int(parent().par.Atlaswidth.eval()))")
    _expr(atlas_pack, ("resolutionh", "resh"),
          "max(1, int(parent().par.Atlasheight.eval()))")
    _set(atlas_pack, "format", "rgba32float")

    shared_rx = _ensure(comp, "sharedmeminTOP", "RX_SHARED_ATLAS", report,
                        optional=True)
    shared_tx = _ensure(comp, "sharedmemoutTOP", "TX_SHARED_ATLAS", report,
                        optional=True)
    for node in (shared_rx, shared_tx):
        required(node, ("name", "memname"),
                 "str(parent().par.Segmentname.eval()) + '_atlas'", True)
        required(node, "memtype", "global")
        required(node, "format", "rgba32float")
    # At 5-10 Hz, Immediate is a deliberate reliability tradeoff: each forced
    # callback cook completes one write while Active is pulsed, with no hidden
    # second cook required to finish a deferred download.
    required(shared_tx, "downloadtype", "immediate")
    # Shared Mem Out has no send-step parameter. The frame-start callback also
    # force-cooks this node at Sendstep, so it remains demand-independent when
    # the AI role gates every reconstruction/render stage.
    required(shared_tx, "active", False)
    _connect(atlas_pack, shared_tx, report=report, replace=True)

    tcp_rx = _ensure(comp, "touchinTOP", "RX_TCP_ATLAS", report, optional=True)
    tcp_tx = _ensure(comp, "touchoutTOP", "TX_TCP_ATLAS", report, optional=True)
    required(tcp_rx, "address", "str(parent().par.Peeraddress.eval())", True)
    required(tcp_rx, "active",
             "1 if parent().par.Receiveractive.eval() else 0", True)
    required(tcp_rx, "mintarget", 0.01)
    required(tcp_rx, "maxtarget", 0.04)
    required(tcp_rx, "maxqueue", 0.12)
    required(tcp_rx, "port", "int(parent().par.Atlasport.eval())", True)
    required(tcp_rx, "format", "rgba32float")
    required(tcp_tx, "active", "1 if parent().par.Senderactive.eval() else 0", True)
    # Touch Out calls this parameter fps, but it is frames-per-send step.
    required(tcp_tx, "fps", "max(1, int(parent().par.Sendstep.eval()))", True)
    required(tcp_tx, "videocodec", "uncompressed")
    required(tcp_tx, "alwayscook", True)
    required(tcp_tx, "port", "int(parent().par.Atlasport.eval())", True)
    required(tcp_tx, "format", "rgba32float")
    _connect(atlas_pack, tcp_tx, report=report, replace=True)

    info = _ensure(comp, "infoCHOP", "RX_TCP_ATLAS_INFO", report, optional=True)
    if info is not None and tcp_rx is not None:
        required(info, ("op", "operator"), tcp_rx.path)
        _style(info, 330, -250, (0.15, 0.32, 0.47),
               "Receiver connected / receive_fps / queue_size", 150, 70)

    atlas_route = _ensure(comp, "switchTOP", "ATLAS_ROUTE", report)
    _connect(shared_rx, atlas_route, 0, 0, report, replace=True)
    _connect(tcp_rx, atlas_route, 1, 0, report, replace=True)
    _set(atlas_route, "index", 0)
    unpack_rgb = _glsl(comp, "UNPACK_ATLAS_RGB", "transport_unpack_rgb",
                       [atlas_route], report)
    unpack_depth = _glsl(comp, "UNPACK_ATLAS_DEPTH", "transport_unpack_depth",
                         [atlas_route], report)
    unpack_confidence = _glsl(
        comp, "UNPACK_ATLAS_CONFIDENCE", "transport_unpack_confidence",
        [atlas_route], report)
    unpack_mask = _glsl(comp, "UNPACK_ATLAS_MASK", "transport_unpack_mask",
                        [atlas_route], report)
    for node in (unpack_rgb, unpack_depth, unpack_confidence, unpack_mask):
        _set(node, "outputresolution", "custom")
        _set(node, "resmult", False)
        _expr(node, ("resolutionw", "resw"),
              "max(1, int(parent().par.Atlaswidth.eval()) // 2)")
        _expr(node, ("resolutionh", "resh"),
              "max(1, int(parent().par.Atlasheight.eval()))")
    _set(unpack_rgb, "format", "rgba16float")
    _set(unpack_depth, "format", "mono32float")
    _set(unpack_confidence, "format", "mono16float")
    _set(unpack_mask, "format", "mono16float")

    rgb_route = _ensure(comp, "switchTOP", "RGB_ROUTE", report)
    depth_route = _ensure(comp, "switchTOP", "DEPTH_ROUTE", report)
    confidence_route = _ensure(comp, "switchTOP", "CONFIDENCE_ROUTE", report)
    mask_route = _ensure(comp, "switchTOP", "MASK_ROUTE", report)
    _connect(local_rgb, rgb_route, 0, 0, report, replace=True)
    _connect(unpack_rgb, rgb_route, 1, 0, report, replace=True)
    _connect(local_depth, depth_route, 0, 0, report, replace=True)
    _connect(unpack_depth, depth_route, 1, 0, report, replace=True)
    _connect(local_confidence, confidence_route, 0, 0, report, replace=True)
    _connect(unpack_confidence, confidence_route, 1, 0, report, replace=True)
    _connect(local_mask, mask_route, 0, 0, report, replace=True)
    _connect(unpack_mask, mask_route, 1, 0, report, replace=True)
    _set(rgb_route, "index", 0)
    _set(depth_route, "index", 0)
    _set(confidence_route, "index", 0)
    _set(mask_route, "index", 0)
    validity = _glsl(comp, "COMBINE_ROUTED_VALIDITY", "validity_combine",
                     [mask_route, confidence_route], report, False)
    _out_top(comp, "OUT_RGB", rgb_route, 0, report)
    _out_top(comp, "OUT_DEPTH", depth_route, 1, report)
    _out_top(comp, "OUT_CONFIDENCE", validity, 2, report)
    _out_top(comp, "OUT_MASK", mask_route, 3, report)

    _table(comp, "TRANSPORT_CONTRACT", [
        ["mode", "frame", "endpoint", "contract"],
        ["local", "no copy", "same process", "RGB + raw depth + confidence + mask"],
        ["shared_memory", "atomic", "Segmentname_atlas", "RGBA32F: left RGB; right R=raw depth G=confidence B=mask"],
        ["touch_tcp", "atomic", "Atlasport", "uncompressed RGBA32F atlas; no depth clamp"],
        ["cadence", "Sendfps target", "Sendstep frame modulus", "project.cookRate derived"],
        ["metadata", "FRAME_STATE_CONTRACT", "frame/session/timestamp + calibration id/digest", "adapter/WorldBus boundary"],
        ["scope", "direct image bridge", "Touch TCP num_received_frames is transport-arrival preview only", "explicit sidecar; WorldBus required for producer metadata"],
    ], report)
    _table(comp, "FRAME_STATE_CONTRACT", [
        ["field", "type", "rule"],
        ["version", "string", "flexgpu-frame-state/v1"],
        ["session_id", "identifier", "new session retires previous high-water mark"],
        ["frame_id", "integer", "strictly increasing within session"],
        ["timestamp_ns", "integer", "strictly increasing; freshness clock"],
        ["calibration_id", "identifier", "must match loaded calibration"],
        ["calibration_digest", "lowercase sha256", "must match canonical calibration content"],
        ["fallback", "transport arrival preview", "TCP counter is not producer-generation identity; metadata-less Shared Mem fails closed"],
    ], report)
    _text(comp, "README_FIRST", "ROLE-AWARE ATOMIC PREVIEW BRIDGE\n\n"
          "Single topology routes RGB/depth locally without a copy. The turnkey "
          "dual_local path uses loopback Touch TCP; its advanced Shared Mem mode "
          "uses one global RGBA32F atlas plus explicit frame-state metadata. "
          "dual_network uses one uncompressed Touch Out/In atlas on Atlasport. "
          "The atlas left half is RGB "
          "and right-half R/G/B carry raw calibrated depth, confidence, and mask, "
          "making all image planes atomic without clamping metric/disparity values. "
          "Touch Out's fps parameter is a frame-step value derived from "
          "project.cookRate and Sendfps. The frame-start callback force-cooks "
          "Shared Mem Out at the same step even when world stages are disabled. "
          "FRAME_STATE_CONTRACT defines frame/session/timestamp and canonical "
          "calibration identity. Local adapters are sampled directly. Touch TCP's "
          "num_received_frames is a transport-arrival preview counter, not "
          "producer-generation identity. Metadata-less Shared Mem fails closed; "
          "an explicit frame-state sidecar or WorldBus is required for exact "
          "producer lifecycle, camera matrices, heartbeats, and controls.", report)
    try:
        comp.store("managed_transport_bridge", True)
    except Exception:
        pass
    return comp


def _build_reconstruction(parent, report):
    comp = _ensure(parent, "baseCOMP", "RECONSTRUCTION", report)
    _style(comp, -1000, 300, (0.20, 0.42, 0.56),
           "Depth unprojection: RGB/depth -> metric position texture", 235, 110)
    rgb = _in_top(comp, "RGB_IN", 0, report)
    depth = _in_top(comp, "DEPTH_IN", 1, report)
    confidence = _in_top(comp, "CONFIDENCE_IN", 2, report)
    page = _page(comp, "Geometry")
    _custom(comp, page, "Int", "Geometryresolution", 384,
            label="Geometry Resolution")
    _custom(comp, page, "Menu", "Depthmode", "normalized",
            ("normalized", "metric", "inverse"), label="Depth Convention")
    _custom(comp, page, "Float", "Depthscale", 1.0, label="Depth Scale")
    _custom(comp, page, "Float", "Depthbias", 0.0, label="Depth Bias")
    _custom(comp, page, "Float", "Nearmetres", 0.35, label="Near (metres)")
    _custom(comp, page, "Float", "Farmetres", 4.50, label="Far (metres)")
    _custom(comp, page, "Float", "Fxnormalized", 0.0,
            label="fx / image width (0 = 60 degree default)")
    _custom(comp, page, "Float", "Fynormalized", 0.0,
            label="fy / image height (0 = 60 degree default)")
    _custom(comp, page, "Float", "Cxnormalized", 0.5, label="cx / image width")
    _custom(comp, page, "Float", "Cynormalized", 0.5, label="cy / image height")
    _custom(comp, page, "Str", "Cameratoworld0", "1 0 0 0", label="Camera to World row 0")
    _custom(comp, page, "Str", "Cameratoworld1", "0 1 0 0", label="Camera to World row 1")
    _custom(comp, page, "Str", "Cameratoworld2", "0 0 1 0", label="Camera to World row 2")
    _custom(comp, page, "Str", "Cameratoworld3", "0 0 0 1", label="Camera to World row 3")
    _custom(comp, page, "Int", "Calibrationepoch", 0,
            label="Calibration Epoch")
    # Version 1.0.0 created COLOR_ALIGNED as a Null TOP. _ensure() deliberately
    # preserves existing/unknown nodes, so reusing that name cannot migrate its
    # operator type and Common-page resolution values remain ineffective. Keep
    # the legacy node untouched and use an unambiguous managed Resolution TOP.
    color = _ensure(comp, "resolutionTOP", "COLOR_ALIGNED_RESIZE", report)
    _connect(rgb, color, report=report, replace=True)
    _set(color, "outputresolution", "custom")
    _set(color, "resmult", False)
    _expr(color, ("resolutionw", "resw"), "parent().par.Geometryresolution")
    _expr(color, ("resolutionh", "resh"), "parent().par.Geometryresolution")
    confidence_aligned = _ensure(comp, "resolutionTOP", "CONFIDENCE_ALIGNED_RESIZE", report)
    _connect(confidence, confidence_aligned, report=report, replace=True)
    _set(confidence_aligned, "outputresolution", "custom")
    _set(confidence_aligned, "resmult", False)
    _expr(confidence_aligned, ("resolutionw", "resw"), "parent().par.Geometryresolution")
    _expr(confidence_aligned, ("resolutionh", "resh"), "parent().par.Geometryresolution")
    _set(confidence_aligned, "format", "mono16float")
    position = _glsl(comp, "depth_to_position", "depth_to_position",
                     [color, depth, confidence_aligned], report, True)
    # Repair occupied 1.0.0 internal wires: both the shader and OUT_COLOR may
    # still point at the preserved legacy COLOR_ALIGNED Null TOP.
    _connect(color, position, 0, 0, report, replace=True)
    _connect(depth, position, 1, 0, report, replace=True)
    _connect(confidence_aligned, position, 2, 0, report, replace=True)
    _out_top(comp, "OUT_POSITION", position, 0, report)
    color_out = _out_top(comp, "OUT_COLOR", color, 1, report)
    confidence_out = _out_top(comp, "OUT_CONFIDENCE", confidence_aligned, 2, report)
    _connect(color, color_out, 0, 0, report, replace=True)
    _connect(confidence_aligned, confidence_out, 0, 0, report, replace=True)
    _table(comp, "OUTPUT_CONTRACT", [["output", "contract"],
        ["OUT_POSITION", TOP_CONTRACTS["POSITION"]],
        ["OUT_COLOR", TOP_CONTRACTS["COLOR"]],
        ["OUT_CONFIDENCE", TOP_CONTRACTS["CONFIDENCE"]]], report)
    return comp


def _build_sensor(parent, report):
    comp = _ensure(parent, "baseCOMP", "SENSOR_INTERACTION", report)
    _style(comp, -730, 300, (0.18, 0.46, 0.34),
           "Animated fallback sensor; later replace at the same TOP contracts", 245, 110)
    position = _in_top(comp, "WORLD_POSITION_IN", 0, report)
    page = _page(comp, "Sensor")
    _custom(comp, page, "Menu", "Mode", "simulated",
            ("simulated", "replay", "depth_sensor", "disabled"))
    _custom(comp, page, "Float", "Interactionradius", 0.55,
            label="Interaction Radius (metres)")
    _custom(comp, page, "Float", "Forcegain", 1.0, label="Force Gain")
    _custom(comp, page, "Float", "Sensoragems", -1.0,
            label="Sensor Age (ms; -1 unknown)")
    _custom(comp, page, "Int", "Sensorframeid", -1, label="Sensor Frame ID")
    _custom(comp, page, "Str", "Sensortoworld0", "1 0 0 0", label="Sensor to World row 0")
    _custom(comp, page, "Str", "Sensortoworld1", "0 1 0 0", label="Sensor to World row 1")
    _custom(comp, page, "Str", "Sensortoworld2", "0 0 1 0", label="Sensor to World row 2")
    _custom(comp, page, "Str", "Sensortoworld3", "0 0 0 1", label="Sensor to World row 3")

    circle = _ensure(comp, "circleTOP", "SIMULATED_SENSOR_MASK", report, optional=True)
    if circle is None:
        circle = _ensure(comp, "noiseTOP", "SIMULATED_SENSOR_MASK_FALLBACK", report)
    _set_resolution(circle, 384, 384)
    _set(circle, "radiusx", 0.16)
    _set(circle, "radiusy", 0.16)
    _set(circle, "radiusunit", "fraction")
    _set(circle, "centerunit", "fraction")
    # Circle TOP uses (0, 0), not (0.5, 0.5), for image center.
    _expr(circle, "centerx", "0.24 * math.sin(absTime.seconds * 0.73)")
    _expr(circle, "centery", "0.18 * math.cos(absTime.seconds * 0.91)")

    disabled_zero = _ensure(comp, "constantTOP", "DISABLED_SENSOR_ZERO", report)
    _set_resolution(disabled_zero, 384, 384)
    for names in (("colorr", "color1r"), ("colorg", "color1g"),
                  ("colorb", "color1b"), ("colora", "alpha")):
        _set(disabled_zero, names, 0.0)

    replay = _ensure(comp, "baseCOMP", "REPLAY_SENSOR_ADAPTER", report)
    _style(replay, -60, 140, (0.36, 0.33, 0.20),
           "Optional recorded mask/depth source; disabled in simulated mode", 230, 105)
    replay_page = _page(replay, "Adapter")
    _custom(replay, replay_page, "Toggle", "Enabled", False)
    replay_mask = _ensure(replay, "constantTOP", "REPLACE_WITH_REPLAY_MASK", report)
    _set_resolution(replay_mask, 384, 384)
    _set(replay_mask, ("colora", "alpha"), 0.0)
    _out_top(replay, "OUT_MASK", replay_mask, 0, report)
    replay_position = _ensure(replay, "constantTOP", "REPLACE_WITH_REPLAY_POSITION", report)
    _set_resolution(replay_position, 384, 384)
    _set(replay_position, ("colora", "alpha"), 0.0)
    _out_top(replay, "OUT_POSITION", replay_position, 1, report)

    sensor_adapter = _ensure(comp, "baseCOMP", "DEPTH_SENSOR_ADAPTER", report)
    _style(sensor_adapter, 180, 140, (0.25, 0.40, 0.32),
           "Local hardware adapter: output sensor-local metric-position RGBA", 270, 105)
    adapter_page = _page(sensor_adapter, "Adapter")
    _custom(sensor_adapter, adapter_page, "Toggle", "Enabled", False)
    _custom(sensor_adapter, adapter_page, "Str", "Positioncontract",
            TOP_CONTRACTS["SENSOR_POSITION"])
    adapter_position = _ensure(sensor_adapter, "constantTOP",
                               "REPLACE_WITH_CALIBRATED_SENSOR_POSITION", report)
    _set_resolution(adapter_position, 384, 384)
    _set(adapter_position, ("colora", "alpha"), 0.0)
    _out_top(sensor_adapter, "OUT_POSITION", adapter_position, 0, report)
    adapter_mask = _ensure(sensor_adapter, "constantTOP",
                           "REPLACE_WITH_SENSOR_MASK", report)
    _set_resolution(adapter_mask, 384, 384)
    _set(adapter_mask, ("colorr", "color1r"), 0.0)
    _out_top(sensor_adapter, "OUT_MASK", adapter_mask, 1, report)
    adapter_confidence = _ensure(sensor_adapter, "constantTOP",
                                 "REPLACE_WITH_SENSOR_CONFIDENCE", report)
    _set_resolution(adapter_confidence, 384, 384)
    _set(adapter_confidence, ("colorr", "color1r"), 1.0)
    _set(adapter_confidence, ("colorg", "color1g"), 1.0)
    _set(adapter_confidence, ("colorb", "color1b"), 1.0)
    _out_top(sensor_adapter, "OUT_CONFIDENCE", adapter_confidence, 2, report)
    depth_anything_bridge = _build_depth_anything_sensor_bridge(
        sensor_adapter, report)
    _wire_depth_anything_sensor_routes(
        sensor_adapter,
        depth_anything_bridge,
        (adapter_position, adapter_mask, adapter_confidence),
        report,
    )
    calibrated_adapter_position = _glsl(
        comp, "CALIBRATE_SENSOR_POSITION", "sensor_to_world",
        [sensor_adapter], report, True)

    mask_switch = _ensure(comp, "switchTOP", "SENSOR_MASK", report)
    _connect(circle, mask_switch, 0, 0, report, replace=True)
    _connect(replay, mask_switch, 1, 0, report, replace=True)
    _connect(sensor_adapter, mask_switch, 2, 1, report, replace=True)
    _connect(disabled_zero, mask_switch, 3, 0, report, replace=True)
    _expr(mask_switch, "index", "parent().par.Mode.menuIndex")
    simulated_confidence = _ensure(comp, "constantTOP",
                                   "SIMULATED_SENSOR_CONFIDENCE", report)
    replay_confidence = _ensure(comp, "constantTOP",
                                "REPLAY_SENSOR_CONFIDENCE", report)
    for node in (simulated_confidence, replay_confidence):
        _set_resolution(node, 384, 384)
        _set(node, ("colorr", "color1r"), 1.0)
        _set(node, ("colorg", "color1g"), 1.0)
        _set(node, ("colorb", "color1b"), 1.0)
    confidence_switch = _ensure(comp, "switchTOP", "SENSOR_CONFIDENCE", report)
    _connect(simulated_confidence, confidence_switch, 0, 0, report, replace=True)
    _connect(replay_confidence, confidence_switch, 1, 0, report, replace=True)
    _connect(sensor_adapter, confidence_switch, 2, 2, report, replace=True)
    _connect(disabled_zero, confidence_switch, 3, 0, report, replace=True)
    _expr(confidence_switch, "index", "parent().par.Mode.menuIndex")
    simulated_position = _glsl(comp, "sensor_position", "sensor_position",
                               [circle], report, True)
    sensor_position = _ensure(comp, "switchTOP", "SENSOR_POSITION_SOURCE", report)
    _connect(simulated_position, sensor_position, 0, 0, report, replace=True)
    _connect(replay, sensor_position, 1, 1, report, replace=True)
    _connect(calibrated_adapter_position, sensor_position, 2, 0, report, replace=True)
    _connect(disabled_zero, sensor_position, 3, 0, report, replace=True)
    _expr(sensor_position, "index", "parent().par.Mode.menuIndex")
    valid_sensor_position = _glsl(
        comp, "APPLY_SENSOR_VALIDITY", "sensor_validity",
        [sensor_position, mask_switch, confidence_switch], report, True)
    interaction = _glsl(comp, "interaction_field", "interaction_field",
                        [position, valid_sensor_position], report, False)
    _out_top(comp, "OUT_SENSOR_POSITION", valid_sensor_position, 0, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 1, report)
    _out_top(comp, "OUT_SENSOR_MASK", mask_switch, 2, report)
    _text(comp, "CALIBRATION_CONTRACT",
          "DEPTH_SENSOR_ADAPTER/OUT_POSITION must contain sensor-local XYZ "
          "metres in RGB and occupancy in A. OUT_MASK and OUT_CONFIDENCE are "
          "multiplied exactly once after SENSOR_TO_WORLD calibration. Interaction "
          "uses a bounded 8x8 world-space occupancy-primitive search (64 samples "
          "per generated point), an explicit low-resolution SDF approximation.", report)
    return comp


def _build_persistence(parent, report):
    comp = _ensure(parent, "baseCOMP", "TEMPORAL_WORLD", report)
    _style(comp, -450, 300, (0.16, 0.42, 0.42),
           "GPU feedback: carries old points and applies sensor forces", 230, 110)
    position = _in_top(comp, "POSITION_IN", 0, report)
    color = _in_top(comp, "COLOR_IN", 1, report)
    interaction = _in_top(comp, "INTERACTION_IN", 2, report)
    confidence = _in_top(comp, "CONFIDENCE_IN", 3, report)
    page = _page(comp, "Temporal Lifecycle")
    _custom(comp, page, "Float", "Confidencedecay", 0.985,
            label="Confidence Decay")
    _custom(comp, page, "Float", "Ageseconds", 2.0,
            label="Maximum Carried Age (seconds)")
    _custom(comp, page, "Int", "Sourceepoch", 0,
            label="Observed Source Epoch")
    _custom(comp, page, "Int", "Resetcount", 0,
            label="Automatic Reset Count")
    _custom(comp, page, "Toggle", "Newframe", True,
            label="Accepted New Frame (one cook pulse)")
    _custom(comp, page, "Toggle", "Sourcevalid", True,
            label="Accepted Source Fresh / Valid")
    _custom(comp, page, "Float", "Deltaseconds", 1.0 / 60.0,
            label="Bounded Render Delta Seconds")

    state_seed = _ensure(comp, "constantTOP", "STATE_SEED", report)
    _set_resolution(state_seed, 384, 384)
    _set(state_seed, ("colorr", "color1r"), 0.0)
    _set(state_seed, ("colorg", "color1g"), 1.0)
    _set(state_seed, ("colorb", "color1b"), 0.0)
    _set(state_seed, ("colora", "alpha"), 0.0)
    state_feedback = _ensure(comp, "feedbackTOP", "STATE_HISTORY", report)
    _connect(state_seed, state_feedback, 0, 0, report, replace=True)
    frame_control = _ensure(comp, "constantTOP", "FRAME_CONTROL", report)
    _set_resolution(frame_control, 1, 1)
    _set(frame_control, "format", "rgba32float")
    _expr(frame_control, ("colorr", "color1r"),
          "1 if parent().par.Newframe else 0")
    _expr(frame_control, ("colorg", "color1g"),
          "min(0.25, max(0.0, parent().par.Deltaseconds.eval()))")
    _expr(frame_control, ("colorb", "color1b"),
          "1 if parent().par.Sourcevalid else 0")
    _expr(frame_control, ("colora", "color1a", "alpha"),
          "max(0.05, parent().par.Ageseconds.eval())")
    observation = _glsl(
        comp, "TEMPORAL_OBSERVATION", "temporal_observation",
        [position, confidence, frame_control], report, False)
    temporal_state = _glsl(comp, "temporal_state", "temporal_state",
                           [observation, state_feedback], report, False)
    _set(state_feedback, ("targettop", "target"), temporal_state.path)

    feedback = _ensure(comp, "feedbackTOP", "POSITION_HISTORY", report)
    # Feedback TOP still needs a seed input even when its target is set.  The
    # live frame is the deterministic first-frame seed; subsequent frames come
    # from the target TOP below.
    _connect(position, feedback, 0, 0, report, replace=True)
    advected_history = _glsl(
        comp, "ADVECT_HISTORY", "temporal_advect",
        [feedback, interaction, frame_control], report, True)
    persistent = _glsl(comp, "temporal_persistence", "temporal_persistence",
                       [position, advected_history, temporal_state], report, True)
    _set(feedback, ("targettop", "target"), persistent.path)
    color_feedback = _ensure(comp, "feedbackTOP", "COLOR_HISTORY", report)
    _connect(color, color_feedback, 0, 0, report, replace=True)
    persistent_color = _glsl(comp, "temporal_color", "temporal_color",
                             [color, color_feedback, temporal_state], report, False)
    _set(color_feedback, ("targettop", "target"), persistent_color.path)
    shader_info = _ensure(comp, "infoDAT", "TEMPORAL_SHADER_INFO", report, optional=True)
    if shader_info is not None:
        _set(shader_info, ("op", "operator"), persistent.path)
    _out_top(comp, "OUT_POSITION", persistent, 0, report)
    _out_top(comp, "OUT_COLOR", persistent_color, 1, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 2, report)
    _out_top(comp, "OUT_TEMPORAL_STATE", temporal_state, 3, report)
    _text(comp, "RESET_NOTE", "FRAME_CONTROL supplies one-cook new-frame, source-valid, and "
          "bounded dt semantics, so a held source texture decays/ages without being "
          "reabsorbed. POSITION_HISTORY, COLOR_HISTORY, and STATE_HISTORY are reset "
          "automatically when source session, geometry resolution, calibration, or "
          "adapter identity changes. Pulse all three Reset parameters after any "
          "untracked manual contract change.", report)
    return comp


def _build_completion(parent, report):
    comp = _ensure(parent, "baseCOMP", "COMPLETION", report)
    _style(comp, -170, 300, (0.56, 0.37, 0.15),
           "Fog/thickness, procedural backfill, or hybrid completion", 240, 110)
    position = _in_top(comp, "POSITION_IN", 0, report)
    color = _in_top(comp, "COLOR_IN", 1, report)
    interaction = _in_top(comp, "INTERACTION_IN", 2, report)
    page = _page(comp, "Completion")
    _custom(comp, page, "Menu", "Mode", "hybrid", ("fog", "procedural", "hybrid"))
    _custom(comp, page, "Float", "Fogdensity", 0.35, label="Fog Density")
    _custom(comp, page, "Float", "Disocclusionradius", 2.0,
            label="Disocclusion Radius (pixels)")
    _custom(comp, page, "Float", "Fognoise", 0.50,
            label="Fog Noise Amount")
    _custom(comp, page, "Float", "Proceduralmix", 0.72,
            label="Procedural Mix")

    procedural_position = _glsl(comp, "procedural_backfill", "procedural_backfill",
                                [position, interaction], report, True)
    fog_color = _glsl(comp, "fog_completion", "fog_completion",
                      [position, color], report, False)
    procedural_color = _glsl(comp, "procedural_color", "procedural_color",
                             [position, procedural_position, color], report, False)
    hybrid_color = _glsl(comp, "hybrid_completion", "hybrid_completion",
                         [position, fog_color, procedural_color], report, False)

    position_switch = _ensure(comp, "switchTOP", "COMPLETED_POSITION", report)
    color_switch = _ensure(comp, "switchTOP", "COMPLETED_COLOR", report)
    for index, source in enumerate((position, procedural_position, procedural_position)):
        _connect(source, position_switch, index, 0, report, replace=True)
    for index, source in enumerate((fog_color, procedural_color, hybrid_color)):
        _connect(source, color_switch, index, 0, report, replace=True)
    _expr(position_switch, "index", "parent().par.Mode.menuIndex")
    _expr(color_switch, "index", "parent().par.Mode.menuIndex")
    _out_top(comp, "OUT_POSITION", position_switch, 0, report)
    _out_top(comp, "OUT_COLOR", color_switch, 1, report)
    return comp


def _build_render_contract(parent, report):
    comp = _ensure(parent, "baseCOMP", "RENDER_CONTRACT", report)
    _style(comp, 110, 350, (0.22, 0.37, 0.56),
           "Stable render/network WorldBus texture boundary", 230, 105)
    position = _in_top(comp, "POSITION_IN", 0, report)
    color = _in_top(comp, "COLOR_IN", 1, report)
    interaction = _in_top(comp, "INTERACTION_IN", 2, report)
    temporal_state = _in_top(comp, "TEMPORAL_STATE_IN", 3, report)
    _out_top(comp, "OUT_POSITION", position, 0, report)
    _out_top(comp, "OUT_COLOR", color, 1, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 2, report)
    _out_top(comp, "OUT_TEMPORAL_STATE", temporal_state, 3, report)
    _table(comp, "TOP_CONTRACTS", [["name", "contract"]] +
           [[name, TOP_CONTRACTS[name]] for name in
            ("POSITION", "COLOR", "INTERACTION", "TEMPORAL_STATE")], report)
    return comp


def _build_point_render(parent, report):
    comp = _ensure(parent, "baseCOMP", "POINT_RENDER", report)
    _style(comp, 390, 350, (0.38, 0.49, 0.17),
           "TOP-to-POP point cloud with center and stereo Render Simple views", 260, 115)
    position = _in_top(comp, "POSITION_IN", 0, report)
    color = _in_top(comp, "COLOR_IN", 1, report)
    page = _page(comp, "Render")
    _custom(comp, page, "Int", "Maxpoints", 120000, label="Maximum Points")
    _custom(comp, page, "Float", "Pointsize", 3.0, label="Point Thickness")
    _custom(comp, page, "Float", "Pointkeep", 0.68,
            label="Visible Point Fraction")
    _custom(comp, page, "Float", "Pointopacity", 0.92,
            label="Point Opacity")
    _custom(comp, page, "Float", "Ipdmetres", 0.064,
            label="Preview Inter-Pupillary Distance (metres)")

    points = _ensure(comp, "toptoPOP", "POSITION_TO_POINTS", report, optional=True)
    point_glyph = _glsl(comp, "POINT_GLYPH", "point_glyph", [], report, False)
    _set_resolution(point_glyph, 64, 64)
    point_material = _ensure(comp, "pointspriteMAT", "POINT_SPRITE_MATERIAL",
                             report, optional=True)
    if point_material is not None:
        _expr(point_material, "pointsize", "parent().par.Pointsize")
        _expr(point_material, "alpha", "parent().par.Pointopacity")
        _set(point_material, "colormap", point_glyph.path)
        _set(point_material, "colormapfilter", "linear")
        _set(point_material, "blending", True)
        _set(point_material, "srcblend", "sa")
        _set(point_material, "destblend", "omsa")
        _set(point_material, "alphatest", True)
        _set(point_material, "alphafunc", "greater")
        _set(point_material, "alphathreshold", 0.01)
    render_center = None
    render_left = None
    render_right = None
    if points is not None:
        _set(points, "rgba", "pactive")
        _set(points, "input0top", position.path)
        _set(points, "input0chanscope", "r g b a")
        _set(points, "input0attrscope", "P P P P")
        _set(points, "input0filter", "nearest")
        # Store source color on each point. Using the full source image as the
        # Point Sprite MAT texture makes every point a tiny textured square and
        # visually collapses the cloud back into a dense image plate.
        if _set_sequence_blocks(points, "input", 2):
            _set(points, "input1top", color.path)
            _set(points, "input1chanscope", "r g b a")
            _set(points, "input1attrscope", "Color Color Color Color")
            _set(points, "input1filter", "nearest")
        _set(points, "surftype", "points")
        _set(points, "texture", "point")
        _set(points, "maxpointsenable", True)
        _expr(points, "maxpoints", "parent().par.Maxpoints")

        # TouchDesigner maps the Thin Random slider cubically. Convert the
        # public linear keep fraction so 0.68 means approximately 68% of active
        # points, using a stable seed to avoid frame-to-frame sparkle.
        point_source = points
        point_thin = _ensure(comp, "deletePOP", "VISIBLE_POINT_THIN", report,
                             optional=True)
        if point_thin is not None:
            _connect(points, point_thin, report=report, replace=True)
            _set(point_thin, "entity", "point")
            _set(point_thin, "thinenabled", True)
            _set(point_thin, "thininvert", False)
            _expr(
                point_thin,
                "thinrandom",
                "1.0 - pow(max(0.0, 1.0 - parent().par.Pointkeep.eval()), "
                "1.0 / 3.0)",
            )
            _set(point_thin, "thinrandomseed", 19)
            point_source = point_thin

        # Render Simple cannot translate a camera on X; its old eye path moved
        # and toe-in rotated the geometry, and Normalize Geo destroyed metres.
        # A managed Geometry/Camera/Render path preserves world scale and uses
        # parallel per-eye Camera COMPs with +/- IPD/2 shifts.
        geo = _ensure(comp, "geometryCOMP", "POINT_WORLD_GEO", report,
                      optional=True)
        selected = None
        if geo is not None:
            selected = _ensure(geo, "selectPOP", "SELECT_POINT_WORLD", report,
                               optional=True)
            if selected is not None:
                _set(selected, "pop", point_source.path)
                try:
                    selected.render = True
                    selected.display = True
                except Exception:
                    pass
            # Disable only TouchDesigner's known default primitive, never an
            # artist's unknown nodes inside the managed geometry boundary.
            default_primitive = _child(geo, "torus1")
            if default_primitive is not None:
                try:
                    default_primitive.render = False
                    default_primitive.display = False
                except Exception:
                    pass
            if point_material is not None:
                _set(geo, "material", point_material.path)

        cameras = {}
        for camera_name, shift_expression in (
            ("CAMERA_CENTER_METRIC", "0.0"),
            ("CAMERA_LEFT_METRIC", "-parent().par.Ipdmetres.eval() * 0.5"),
            ("CAMERA_RIGHT_METRIC", "parent().par.Ipdmetres.eval() * 0.5"),
        ):
            camera = _ensure(comp, "cameraCOMP", camera_name, report,
                             optional=True)
            if camera is not None:
                _expr(camera, "tx", shift_expression)
                _set(camera, "ty", 0.0)
                _set(camera, "tz", 0.0)
                _set(camera, "rx", 0.0)
                _set(camera, "ry", 0.0)
                _set(camera, "rz", 0.0)
                _set(camera, "fov", 55.0)
                _set(camera, "near", 0.05)
                _set(camera, "far", 100.0)
                _set(camera, "ipdshift", 0.0)
            cameras[camera_name] = camera

        def make_metric_render(name, camera):
            if geo is None or selected is None or camera is None:
                return None
            node = _ensure(comp, "renderTOP", name, report, optional=True)
            if node is None:
                return None
            _set(node, "geometry", geo.path)
            _set(node, "camera", camera.path)
            _set(node, "lights", "")
            if point_material is not None:
                _set(node, "overridemat", point_material.path)
            _set(node, "bgcolorr", 0.005)
            _set(node, "bgcolorg", 0.009)
            _set(node, "bgcolorb", 0.018)
            _set(node, "bgcolora", 0.0)
            _set_resolution(node, 1280, 720)
            return node

        render_center = make_metric_render(
            "METRIC_RENDER_CENTER", cameras.get("CAMERA_CENTER_METRIC"))
        render_left = make_metric_render(
            "METRIC_RENDER_LEFT_EYE", cameras.get("CAMERA_LEFT_METRIC"))
        render_right = make_metric_render(
            "METRIC_RENDER_RIGHT_EYE", cameras.get("CAMERA_RIGHT_METRIC"))

        # A stock Render Simple center view remains a safe pre-2025 fallback.
        # It deliberately produces mono eyes: fake toe-in stereo is worse than
        # an honest mono fallback. Metric geometry is never normalized.
        if render_center is None:
            legacy = _ensure(comp, "rendersimpleTOP", "METRIC_MONO_FALLBACK",
                             report, optional=True)
            if legacy is not None:
                _set(legacy, "pop", points.path)
                _set(legacy, "colormap", color.path)
                if point_material is not None:
                    _set(legacy, "materialsource", "matnode")
                    _set(legacy, "mat", point_material.path)
                _set(legacy, "normalizegeo", False)
                _set(legacy, "ortho", False)
                _set(legacy, "fov", 55.0)
                _set(legacy, "camdistance", 0.0)
                _set(legacy, "geotranslatex", 0.0)
                _set(legacy, "georotatey", 0.0)
                _set(legacy, "bgcolorr", 0.005)
                _set(legacy, "bgcolorg", 0.009)
                _set(legacy, "bgcolorb", 0.018)
                _set(legacy, "bgcolora", 0.0)
                _set_resolution(legacy, 1280, 720)
            render_center = legacy
            render_left = legacy
            render_right = legacy

    # A valid color TOP fallback makes the project inspectable even if opened in
    # a pre-POP TouchDesigner build.  In 2025.32820 the switches select renders.
    center_switch = _ensure(comp, "switchTOP", "CENTER_OR_FALLBACK", report)
    left_switch = _ensure(comp, "switchTOP", "LEFT_OR_FALLBACK", report)
    right_switch = _ensure(comp, "switchTOP", "RIGHT_OR_FALLBACK", report)
    for switch, rendered in ((center_switch, render_center),
                             (left_switch, render_left),
                             (right_switch, render_right)):
        _connect(color, switch, 0, 0, report, replace=True)
        if rendered is not None:
            _connect(rendered, switch, 1, 0, report, replace=True)
            _set(switch, "index", 1)
        else:
            _set(switch, "index", 0)
    _out_top(comp, "OUT_CENTER", center_switch, 0, report)
    _out_top(comp, "OUT_LEFT_EYE", left_switch, 1, report)
    _out_top(comp, "OUT_RIGHT_EYE", right_switch, 2, report)
    _table(comp, "RENDER_PATH", [
        ["stage", "operator", "contract"],
        ["unpack", "POSITION_TO_POINTS (TOP to POP)", "P + active plus aligned per-point Color"],
        ["spacing", "VISIBLE_POINT_THIN (Delete POP)", "stable random keep; 68% default"],
        ["glyph", "POINT_GLYPH + POINT_SPRITE_MATERIAL", "soft circular alpha; no source-image sprite texture"],
        ["thickness", "POINT_SPRITE_MATERIAL", "Pointsize and Pointopacity controls"],
        ["metric geometry", "POINT_WORLD_GEO/SELECT_POINT_WORLD", "no normalization; XYZ remain metres"],
        ["center", "METRIC_RENDER_CENTER + CAMERA_CENTER_METRIC", TOP_CONTRACTS["INSTALLATION"]],
        ["stereo", "METRIC_RENDER_LEFT/RIGHT + parallel Camera COMPs", "+/- Ipdmetres/2; no toe-in"],
        ["fallback", "*_OR_FALLBACK input 0", "completed color TOP"],
    ], report)
    return comp


def _build_installation(parent, report):
    comp = _ensure(parent, "baseCOMP", "INSTALLATION_OUTPUT", report)
    _style(comp, 690, 430, (0.46, 0.48, 0.14),
           "Visually inspectable point render plus disocclusion fog plate", 255, 110)
    point_render = _in_top(comp, "POINT_RENDER_IN", 0, report)
    fog_plate = _in_top(comp, "FOG_PLATE_IN", 1, report)
    page = _page(comp, "View Completion")
    _custom(comp, page, "Float", "Fogdensity", 0.35, label="View Fog Density")
    _custom(comp, page, "Float", "Fogradius", 2.0, label="View Fog Radius")
    grade = _glsl(comp, "installation_grade", "installation_grade",
                  [point_render, fog_plate], report, False)
    _set_resolution(grade, 1280, 720)
    output = _ensure(comp, "nullTOP", "OUT_INSTALLATION", report)
    _connect(grade, output, report=report, replace=True)
    _out_top(comp, "out1", output, 0, report)
    return comp


def _build_stereo(parent, report):
    comp = _ensure(parent, "baseCOMP", "STEREO_PREVIEW", report)
    _style(comp, 690, 250, (0.39, 0.46, 0.18),
           "Desktop left/right/SBS preview; no OpenVR dependency", 245, 105)
    left = _in_top(comp, "LEFT_IN", 0, report)
    right = _in_top(comp, "RIGHT_IN", 1, report)
    page = _page(comp, "View Completion")
    _custom(comp, page, "Float", "Fogdensity", 0.35, label="Per-eye Fog Density")
    _custom(comp, page, "Float", "Fogradius", 2.0, label="Per-eye Fog Radius")
    left_grade = _glsl(comp, "GRADE_LEFT_EYE", "view_completion", [left], report, False)
    right_grade = _glsl(comp, "GRADE_RIGHT_EYE", "view_completion", [right], report, False)
    layout = _ensure(comp, "layoutTOP", "STEREO_SIDE_BY_SIDE", report, optional=True)
    if layout is None:
        layout = _ensure(comp, "compositeTOP", "STEREO_SIDE_BY_SIDE_FALLBACK", report)
    _connect(left_grade, layout, 0, 0, report, replace=True)
    _connect(right_grade, layout, 1, 0, report, replace=True)
    _set(layout, ("align", "direction"), "horizontal")
    _set_resolution(layout, 2560, 720)
    _out_top(comp, "OUT_LEFT_EYE", left_grade, 0, report)
    _out_top(comp, "OUT_RIGHT_EYE", right_grade, 1, report)
    _out_top(comp, "OUT_STEREO_SBS", layout, 2, report)
    _text(comp, "README_FIRST", "This is a headset-independent stereo preview. "
          "The preview uses parallel metric Camera COMPs and does not consume a "
          "headset pose, per-eye projection matrices, hidden-area mesh, late-latch "
          "timing, or compositor textures. An OpenXR/OpenVR adapter must provide "
          "those per-frame values, consume the same metric point world, and replace "
          "the complete camera/output layer; this SBS TOP is not a headset runtime.", report)
    _table(comp, "HEADSET_ADAPTER_CONTRACT", [
        ["required input", "units / convention"],
        ["world_from_eye_left/right", "right-handed row-major metres"],
        ["projection_left/right", "runtime-supplied clip-space matrices"],
        ["predicted_display_time", "runtime monotonic timestamp"],
        ["submission", "headset compositor texture contract"],
    ], report)
    return comp


def _build_telemetry(parent, watched, report):
    comp = _ensure(parent, "baseCOMP", "TELEMETRY", report)
    _style(comp, 390, 120, (0.42, 0.28, 0.23),
           "Actual Info CHOP metrics plus a documented performance DAT", 235, 105)
    info_nodes = []
    for index, (name, node) in enumerate(watched):
        info = _ensure(comp, "infoCHOP", "INFO_%s" % name, report, optional=True)
        if info is not None:
            _set(info, ("op", "operator"), node.path)
            info_nodes.append(info)
    merge = _ensure(comp, "mergeCHOP", "PERFORMANCE_METRICS", report, optional=True)
    if merge is not None:
        for index, info in enumerate(info_nodes):
            _connect(info, merge, index, 0, report, replace=True)
        metrics = _ensure(comp, "nullCHOP", "OUT_PERFORMANCE", report, optional=True)
        _connect(merge, metrics, report=report, replace=True)
        out = _ensure(comp, "outCHOP", "out1", report, optional=True)
        _connect(metrics, out, report=report, replace=True)
    status_dat = _ensure(comp, "infoDAT", "OPERATOR_STATUS", report, optional=True)
    if status_dat is not None:
        _set(status_dat, ("op", "operator"), watched[-1][1].path)
    _table(comp, "TELEMETRY_CONTRACT", [
        ["metric", "source", "operator action"],
        ["cook_time", "Info CHOPs", "lower geometry resolution if over budget"],
        ["cook_frame", "Info CHOPs", "detect stale async/model frames"],
        ["gpu_memory", "external nvidia-smi/monitor", "drop point budget before outputs"],
        ["world_age", "future model adapter", "drop stale AI frames; never queue"],
        ["sensor_age", "future sensor adapter", "fall back to simulated/replay mode"],
        ["target_fps", "launcher tier", "3080=60/72; 4090/5090=60/90"],
    ], report)
    _table(comp, "LIVE_HEALTH", [
        ["metric", "value"],
        ["status", "initializing"],
        ["source_age_ms", "-1"],
        ["sensor_age_ms", "-1"],
        ["source_frame_id", "-1"],
        ["sensor_frame_id", "-1"],
        ["temporal_resets", "0"],
        ["frame_time_ms", "0"],
    ], report)
    return comp


def _build_experimental(parent, report):
    comp = _ensure(parent, "baseCOMP", "EXPERIMENTAL_EXTERNAL_ADAPTERS", report)
    _style(comp, 690, 70, (0.30, 0.25, 0.34),
           "SHARP/Gaussian process boundaries; OFF and non-cooking by default", 260, 110)
    for index, (name, spec) in enumerate(EXPERIMENTAL_ADAPTERS.items()):
        stub = _ensure(comp, "baseCOMP", name, report)
        _style(stub, index * 250, 40, (0.34, 0.22, 0.38),
               "EXTERNAL EXPERIMENT - DISABLED", 220, 95)
        page = _page(stub, "External Adapter")
        _custom(stub, page, "Toggle", "Enabled", spec["default_enabled"])
        _custom(stub, page, "Str", "Contract", spec["contract"])
        placeholder = _ensure(stub, "constantTOP", "DISABLED_PLACEHOLDER", report)
        _set_resolution(placeholder, 64, 64)
        _out_top(stub, "OUT_EXTERNAL", placeholder, 0, report)
        try:
            stub.allowCooking = False
        except Exception:
            pass
        try:
            stub.store("default_enabled", False)
            stub.store("external_only", True)
        except Exception:
            pass
    _text(comp, "README_FIRST", "These are process/transport contracts, not bundled models. "
          "They intentionally do not cook. Enable only after a supervised external "
          "worker and a fresh-frame transport have been configured.", report)
    return comp


def _first_input(node):
    try:
        inputs = node.inputs
        return inputs[0] if inputs else None
    except Exception:
        return None


def install_depth_anything_sensor_bridge(root=None):
    """Install only the default-off, replaceable sensor bridge and routes.

    This bounded local-project installer never rebuilds ``WORKING_PIPELINE``.
    It preserves the adapter's current outputs as disabled fallbacks, refreshes
    only ``DEPTH_ANYTHING_BRIDGE`` plus three named route switches, and does
    not delete operators or load a model/camera SDK inside TouchDesigner.
    """

    global LAST_REPORT
    report = BuildReport()
    LAST_REPORT = report
    if root is None:
        root = _op(ROOT_PATH)
    elif isinstance(root, str):
        root = _op(root)
    if root is None:
        raise RuntimeError("FlexGPU root %s does not exist" % ROOT_PATH)
    adapter = root.op(
        "WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER")
    if adapter is None:
        raise RuntimeError(
            "depth sensor adapter is missing; build the working pipeline first")

    existing_runtime = adapter.op("DEPTH_ANYTHING_BRIDGE/sensor_runtime")
    if existing_runtime is not None:
        try:
            existing_runtime.module.stop(adapter.op("DEPTH_ANYTHING_BRIDGE"))
        except Exception:
            pass

    fallback_names = (
        ("OUT_POSITION", "DEPTH_ANYTHING_POSITION_ROUTE",
         "REPLACE_WITH_CALIBRATED_SENSOR_POSITION"),
        ("OUT_MASK", "DEPTH_ANYTHING_MASK_ROUTE", "REPLACE_WITH_SENSOR_MASK"),
        ("OUT_CONFIDENCE", "DEPTH_ANYTHING_CONFIDENCE_ROUTE",
         "REPLACE_WITH_SENSOR_CONFIDENCE"),
    )
    fallbacks = []
    for output_name, route_name, placeholder_name in fallback_names:
        output = adapter.op(output_name)
        source = _first_input(output)
        if source is not None and str(getattr(source, "name", "")) == route_name:
            source = _first_input(source)
        if source is None:
            source = adapter.op(placeholder_name)
        if source is None:
            raise RuntimeError(
                "could not preserve sensor fallback source for " + output_name)
        fallbacks.append(source)

    bridge = _build_depth_anything_sensor_bridge(adapter, report)
    _wire_depth_anything_sensor_routes(adapter, bridge, tuple(fallbacks), report)
    try:
        bridge.store("depth_anything_bridge_install_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] Depth Anything sensor bridge installed disabled: %s "
          "(%d created, %d reused, %d warnings)" %
          (bridge.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return bridge


def install_moge2_bridge(root=None):
    """Install only the opt-in MoGe-2 branch into an existing working adapter.

    This bounded installer is intended for an artist's local saved project. It
    preserves the four current adapter sources as disabled fallbacks, creates
    or refreshes only ``MOGE2_BRIDGE`` and its route switches, and never rebuilds
    the rest of ``WORKING_PIPELINE``.
    """

    global LAST_REPORT
    report = BuildReport()
    LAST_REPORT = report
    if root is None:
        root = _op(ROOT_PATH)
    elif isinstance(root, str):
        root = _op(root)
    if root is None:
        raise RuntimeError("FlexGPU root %s does not exist" % ROOT_PATH)
    adapter = root.op(
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER")
    if adapter is None:
        raise RuntimeError("StreamDiffusion adapter is missing; build the working pipeline first")

    existing_runtime = adapter.op("MOGE2_BRIDGE/bridge_runtime")
    if existing_runtime is not None:
        try:
            existing_runtime.module.stop(adapter.op("MOGE2_BRIDGE"))
        except Exception:
            pass

    fallback_names = (
        ("OUT_RGB", "MOGE2_RGB_ROUTE", "REPLACE_WITH_STREAMDIFFUSION_RGB"),
        ("OUT_DEPTH", "MOGE2_DEPTH_ROUTE", "REPLACE_WITH_DEPTH_ESTIMATE"),
        ("OUT_CONFIDENCE", "MOGE2_CONFIDENCE_ROUTE", "REPLACE_WITH_CONFIDENCE"),
        ("OUT_MASK", "MOGE2_MASK_ROUTE", "REPLACE_WITH_VALID_MASK"),
    )
    fallbacks = []
    for output_name, route_name, placeholder_name in fallback_names:
        output = adapter.op(output_name)
        source = _first_input(output)
        if source is not None and str(getattr(source, "name", "")) == route_name:
            source = _first_input(source)
        if source is None:
            source = adapter.op(placeholder_name)
        if source is None:
            raise RuntimeError("could not preserve fallback source for " + output_name)
        fallbacks.append(source)

    bridge = _build_moge2_bridge(adapter, fallbacks[0], report)
    _wire_moge2_routes(adapter, bridge, tuple(fallbacks), report)
    try:
        bridge.store("moge2_bridge_install_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] MoGe-2 bridge installed disabled: %s "
          "(%d created, %d reused, %d warnings)" %
          (bridge.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return bridge


def build(root=None):
    """Create or update ``WORKING_PIPELINE`` below *root* and return that COMP.

    ``root`` defaults to ``/project1/flexgpu`` and may be either a COMP or an OP
    path.  The function is idempotent: managed operators are reused and updated;
    no operator is destroyed.  Nothing outside ``WORKING_PIPELINE`` is changed.
    """
    global LAST_REPORT
    report = BuildReport()
    LAST_REPORT = report
    if root is None:
        root = _op(ROOT_PATH)
    elif isinstance(root, str):
        root = _op(root)
    if root is None:
        raise RuntimeError("FlexGPU root %s does not exist" % ROOT_PATH)

    pipeline = _ensure(root, "baseCOMP", PIPELINE_NAME, report)
    _style(pipeline, 0, -430, (0.18, 0.43, 0.37),
           "Built-in working RGB/depth -> persistent interactive point world", 320, 145)
    page = _page(pipeline, "FlexGPU Working Pipeline")
    _custom(pipeline, page, "Str", "Buildversion", BUILD_VERSION, label="Build Version")
    # Existing custom parameters retain their previous value when _custom()
    # reuses them.  Keep the visible/runtime version synchronized on upgrades.
    _set(pipeline, "Buildversion", BUILD_VERSION)
    _custom(pipeline, page, "Pulse", "Rebuild", False)

    sources = _build_sources(pipeline, report)
    role_bridge = _build_role_bridge(pipeline, report)
    reconstruction = _build_reconstruction(pipeline, report)
    sensor = _build_sensor(pipeline, report)
    temporal = _build_persistence(pipeline, report)
    completion = _build_completion(pipeline, report)
    contract = _build_render_contract(pipeline, report)
    point_render = _build_point_render(pipeline, report)
    installation = _build_installation(pipeline, report)
    stereo = _build_stereo(pipeline, report)

    # These wires are owned by the builder and are repaired on every rebuild.
    # This migrates older 1.0.0 networks whose connectors fell back to
    # alphabetical ordering.  StreamDiffusion adapter internals are not forced.
    _connect(sources, role_bridge, 0, 0, report, replace=True)
    _connect(sources, role_bridge, 1, 1, report, replace=True)
    _connect(sources, role_bridge, 2, 2, report, replace=True)
    _connect(sources, role_bridge, 3, 3, report, replace=True)
    _connect(role_bridge, reconstruction, 0, 0, report, replace=True)
    _connect(role_bridge, reconstruction, 1, 1, report, replace=True)
    _connect(role_bridge, reconstruction, 2, 2, report, replace=True)
    _connect(reconstruction, sensor, 0, 0, report, replace=True)
    _connect(reconstruction, temporal, 0, 0, report, replace=True)
    _connect(reconstruction, temporal, 1, 1, report, replace=True)
    _connect(sensor, temporal, 2, 1, report, replace=True)
    _connect(reconstruction, temporal, 3, 2, report, replace=True)
    _connect(temporal, completion, 0, 0, report, replace=True)
    _connect(temporal, completion, 1, 1, report, replace=True)
    _connect(temporal, completion, 2, 2, report, replace=True)
    _connect(completion, contract, 0, 0, report, replace=True)
    _connect(completion, contract, 1, 1, report, replace=True)
    _connect(sensor, contract, 2, 1, report, replace=True)
    _connect(temporal, contract, 3, 3, report, replace=True)
    # POINT_RENDER input 0 is POSITION and input 1 is COLOR. Interaction stays
    # on RENDER_CONTRACT output 2 and is not consumed by the renderer.
    _connect(contract, point_render, 0, 0, report, replace=True)
    _connect(contract, point_render, 1, 1, report, replace=True)
    _connect(point_render, installation, 0, 0, report, replace=True)
    _connect(completion, installation, 1, 1, report, replace=True)
    _connect(point_render, stereo, 0, 1, report, replace=True)
    _connect(point_render, stereo, 1, 2, report, replace=True)

    # Easy-to-find root outputs for projectors, recorders, transports and later
    # VR runtimes.  They mirror the internal stable contract names.
    outputs = (
        ("OUT_POSITION", contract, 0),
        ("OUT_COLOR", contract, 1),
        ("OUT_INTERACTION", contract, 2),
        ("OUT_INSTALLATION", installation, 0),
        ("OUT_LEFT_EYE", stereo, 0),
        ("OUT_RIGHT_EYE", stereo, 1),
        ("OUT_STEREO_PREVIEW", stereo, 2),
        ("OUT_TEMPORAL_STATE", contract, 3),
        ("OUT_SENSOR_POSITION", sensor, 0),
    )
    output_nodes = []
    for index, (name, source, source_index) in enumerate(outputs):
        node = _ensure(pipeline, "nullTOP", name, report)
        _connect(source, node, 0, source_index, report, replace=True)
        _style(node, 1030, 470 - index * 90, (0.18, 0.50, 0.28), name, 185, 70)
        output_nodes.append(node)

    _build_telemetry(pipeline, [
        ("DEPTH_TO_POSITION", reconstruction),
        ("TEMPORAL_WORLD", temporal),
        ("POINT_RENDER", point_render),
        ("INSTALLATION", output_nodes[3]),
    ], report)
    _build_experimental(pipeline, report)
    _table(pipeline, "PIPELINE_MANIFEST", [
        ["field", "value"],
        ["build_version", BUILD_VERSION],
        ["managed_scope", ROOT_PATH + "/" + PIPELINE_NAME],
        ["source_default", "built-in animated RGB/depth demo"],
        ["source_future", "SOURCES/STREAMDIFFUSION_ADAPTER"],
        ["role_bridge", "ROLE_BRIDGE: atomic RGB + raw depth/confidence/mask"],
        ["position_contract", TOP_CONTRACTS["POSITION"]],
        ["renderer", "TOP to POP -> metric Geometry/Camera/Render TOP (TD 2025)"],
        ["installation_output", "OUT_INSTALLATION"],
        ["stereo_output", "OUT_LEFT_EYE, OUT_RIGHT_EYE, OUT_STEREO_PREVIEW"],
        ["openvr_dependency", "none"],
        ["unknown_nodes", "preserved"],
    ], report)
    _text(pipeline, "README_FIRST", "FLEXGPU WORKING PIPELINE\n\n"
          "Open OUT_INSTALLATION for the center point-cloud render and "
          "OUT_STEREO_PREVIEW for a desktop stereo view. The animated RGB/depth "
          "and sensor sources work immediately. Later, replace only the two "
          "labelled TOPs inside SOURCES/STREAMDIFFUSION_ADAPTER and turn on the "
          "source toggles; reconstruction, persistence, completion and outputs "
          "do not change. ROLE_BRIDGE automatically sends or receives those "
          "same RGB/raw-depth/confidence/mask contracts in split roles. "
          "Frame-state metadata controls one-cook acceptance and held-frame decay. "
          "SHARP/Gaussian stubs remain "
          "non-cooking by default.", report)
    try:
        pipeline.store("runtime_pipeline_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] ready: %s (%d created, %d reused, %d warnings)" %
          (pipeline.path, len(report.created), len(report.reused), len(report.warnings)))
    return pipeline


# Importing this file has no TouchDesigner side effects.  Invoke build() from a
# Textport or Text DAT only after the base /project1/flexgpu shell exists.
