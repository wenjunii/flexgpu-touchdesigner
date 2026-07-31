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

import math
import os
import re


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
    "HAND_POSITION": "RGBA32F TOP; sparse RGB=world XYZ metres, A=tracking confidence",
    "INTERACTION": "RGBA16F TOP; RGB=force vector, A=occupancy",
    "INSTALLATION": "RGBA TOP; visually inspectable rendered point world",
    "TRIPLE_DISPLAY": "three RGBA surface TOPs plus a horizontal calibration mosaic",
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
    // TouchDesigner TOP UVs increase from image bottom to image top. Preserve
    // that orientation in camera/world Y so the rendered cloud matches RGB.
    vec3 cameraPosition = vec3((uv.x - cxNormalized) * z / fx,
                               (uv.y - cyNormalized) * z / fy,
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
                         (uv.y - 0.5) * 2.0,
                         -1.15 - 0.20 * occupancy);
    fragColor = TDOutputSwizzle(vec4(position, occupancy));
}
''',
    "mock_hand_positions": r'''// CONTRACT: MOCK HAND CONTROLS -> sparse HAND_POSITION
out vec4 fragColor;

void main()
{
    // Only the first two texels are occupied. The interaction shader samples
    // these exact cells directly, so mock hands add two bounded lookups per
    // generated point instead of another dense occupancy search.
    const float mockHandsEnabled = 0.0; // FLEXGPU_VR_HANDS_ENABLED
    const vec4 mockLeftHand = vec4(-0.28, 0.02, -1.15, 1.0); // FLEXGPU_VR_LEFT_HAND
    const vec4 mockRightHand = vec4(0.28, 0.02, -1.15, 1.0); // FLEXGPU_VR_RIGHT_HAND
    ivec2 cell = ivec2(floor(vUV.st * vec2(32.0)));
    // Input 0 is the deterministic zero seed used by all stock GLSL TOPs.
    vec4 hand = texture(sTD2DInputs[0], vUV.st) * 0.0;
    if (cell.y == 0 && cell.x == 0) {
        hand = mockLeftHand;
    } else if (cell.y == 0 && cell.x == 1) {
        hand = mockRightHand;
    }
    hand.a *= step(0.5, mockHandsEnabled);
    fragColor = TDOutputSwizzle(hand);
}
''',
    "femto_sensor_position": r'''// CONTRACT: ORBBEC POINTCLOUD -> SENSOR_POSITION
out vec4 fragColor;

void main()
{
    vec3 rawPosition = texture(sTD2DInputs[0], vUV.st).rgb;
    // Femto Mega/Orbbec pointcloud data is camera-local XYZ in metres with
    // forward-positive Z. Convert to the FlexGPU sensor convention used by the
    // existing calibration stage: audience X/Y plus forward-negative Z.
    const float femtoMirrorHorizontal = 0.0; // FLEXGPU_FEMTO_MIRROR_HORIZONTAL
    bool finitePoint = all(lessThan(abs(rawPosition), vec3(1000.0)));
    float valid = float(
        finitePoint && rawPosition.z > 0.05 && rawPosition.z < 20.0);
    float orientedX = mix(
        rawPosition.x, -rawPosition.x,
        step(0.5, femtoMirrorHorizontal));
    vec3 sensorPosition = vec3(
        orientedX, rawPosition.y, -rawPosition.z);
    fragColor = TDOutputSwizzle(
        vec4(sensorPosition * valid, valid));
}
''',
    "femto_sensor_validity": r'''// CONTRACT: SENSOR_POSITION alpha -> MASK/CONFIDENCE; audience depth gate
out vec4 fragColor;

void main()
{
    vec4 sensor = texture(sTD2DInputs[0], vUV.st);
    const float femtoNearMetres = 0.25; // FLEXGPU_FEMTO_NEAR_METRES
    const float femtoFarMetres = 12.0; // FLEXGPU_FEMTO_FAR_METRES
    float depthMetres = -sensor.z;
    float validity = clamp(
        sensor.a, 0.0, 1.0) *
        step(femtoNearMetres, depthMetres) *
        step(depthMetres, femtoFarMetres);
    fragColor = TDOutputSwizzle(
        vec4(validity, validity, validity, 1.0));
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
    const float sensorPositionScale = 1.0; // FLEXGPU_SENSOR_POSITION_SCALE
    const float sensorTrimXMetres = 0.0; // FLEXGPU_SENSOR_TRIM_X
    const float sensorTrimYMetres = 0.0; // FLEXGPU_SENSOR_TRIM_Y
    const float sensorTrimZMetres = 0.0; // FLEXGPU_SENSOR_TRIM_Z
    const float sensorTrimYawDegrees = 0.0; // FLEXGPU_SENSOR_TRIM_YAW
    const float sensorTrimPitchDegrees = 0.0; // FLEXGPU_SENSOR_TRIM_PITCH
    const float sensorTrimRollDegrees = 0.0; // FLEXGPU_SENSOR_TRIM_ROLL

    vec3 localPosition = sensor.rgb * sensorPositionScale;
    float yaw = radians(sensorTrimYawDegrees);
    float pitch = radians(sensorTrimPitchDegrees);
    float roll = radians(sensorTrimRollDegrees);
    float cy = cos(yaw);
    float sy = sin(yaw);
    localPosition = vec3(
        cy * localPosition.x + sy * localPosition.z,
        localPosition.y,
        -sy * localPosition.x + cy * localPosition.z);
    float cp = cos(pitch);
    float sp = sin(pitch);
    localPosition = vec3(
        localPosition.x,
        cp * localPosition.y - sp * localPosition.z,
        sp * localPosition.y + cp * localPosition.z);
    float cr = cos(roll);
    float sr = sin(roll);
    localPosition = vec3(
        cr * localPosition.x - sr * localPosition.y,
        sr * localPosition.x + cr * localPosition.y,
        localPosition.z);

    vec4 homogeneous = vec4(localPosition, 1.0);
    vec3 worldPosition = vec3(dot(sensorToWorld0, homogeneous),
                              dot(sensorToWorld1, homogeneous),
                              dot(sensorToWorld2, homogeneous));
    worldPosition += vec3(
        sensorTrimXMetres, sensorTrimYMetres, sensorTrimZMetres);
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
    "interaction_field": r'''// CONTRACT: POSITION + calibrated SENSOR_POSITION + HAND_POSITION -> INTERACTION force + occupancy
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 point = texture(sTD2DInputs[0], uv);
    const float interactionRadiusMetres = 0.55; // FLEXGPU_INTERACTION_RADIUS
    const float interactionFalloff = 1.0; // FLEXGPU_INTERACTION_FALLOFF
    const float forceGain = 1.0; // FLEXGPU_FORCE_GAIN
    const float vrHandGain = 0.65; // FLEXGPU_VR_HAND_GAIN
    // Bounded 32x32 occupancy primitives are sampled across the full sensor,
    // not at the generated point's source UV. This is a practical stock-TD
    // approximation to a low-resolution world-space occupancy/SDF volume.
    // The native Femto gate can leave fewer than 2,000 valid pixels in a
    // 640x576 point cloud. A 16x16 lattice then sees only one or two samples
    // and can lose a moving audience member. The 32x32 lattice is still
    // bounded at the 128x128 interaction resolution and is reliable on the
    // 5090 live profile.
    const int occupancyGridSize = 32; // FLEXGPU_OCCUPANCY_GRID_SIZE
    vec3 accumulatedForce = vec3(0.0);
    float combinedOccupancy = 0.0;
    for (int y = 0; y < occupancyGridSize; ++y) {
        for (int x = 0; x < occupancyGridSize; ++x) {
            vec2 sensorUV = (vec2(float(x), float(y)) + 0.5) /
                            float(occupancyGridSize);
            vec4 sensor = texture(sTD2DInputs[1], sensorUV);
            vec3 delta = point.rgb - sensor.rgb;
            float distanceMetres = length(delta);
            float radialInfluence =
                1.0 - smoothstep(
                    0.0, interactionRadiusMetres, distanceMetres);
            float influence = point.a * sensor.a *
                pow(clamp(radialInfluence, 0.0, 1.0),
                    max(0.25, interactionFalloff));
            vec3 direction = distanceMetres > 1e-5
                ? delta / distanceMetres : vec3(0.0, 0.0, 1.0);
            accumulatedForce += direction * influence;
            combinedOccupancy = max(combinedOccupancy, influence);
        }
    }
    // Mock and future headset adapters publish exactly two sparse hand
    // primitives in cells (0,0) and (1,0). This contract is intentionally
    // independent from the audience depth sensor and remains bounded.
    vec3 handForce = vec3(0.0);
    for (int handIndex = 0; handIndex < 2; ++handIndex) {
        vec2 handUV = (vec2(float(handIndex), 0.0) + 0.5) / 32.0;
        vec4 hand = texture(sTD2DInputs[2], handUV);
        vec3 delta = point.rgb - hand.rgb;
        float distanceMetres = length(delta);
        float radialInfluence =
            1.0 - smoothstep(0.0, interactionRadiusMetres, distanceMetres);
        float influence = point.a * hand.a *
            pow(clamp(radialInfluence, 0.0, 1.0),
                max(0.25, interactionFalloff));
        vec3 direction = distanceMetres > 1e-5
            ? delta / distanceMetres : vec3(0.0, 0.0, 1.0);
        handForce += direction * influence;
        combinedOccupancy = max(combinedOccupancy, influence);
    }
    vec3 force = max(0.0, forceGain) *
        (accumulatedForce / float(occupancyGridSize) +
         handForce * max(0.0, vrHandGain));
    fragColor = TDOutputSwizzle(vec4(force, combinedOccupancy));
}
''',
    "interaction_smoothing": r'''// CONTRACT: INTERACTION + HISTORY -> low-latency smoothed INTERACTION
out vec4 fragColor;

void main()
{
    const float interactionSmoothing = 0.35; // FLEXGPU_INTERACTION_SMOOTHING
    const float interactionResponse = 0.65; // FLEXGPU_INTERACTION_RESPONSE
    const float interactionDecay = 0.5; // FLEXGPU_INTERACTION_DECAY
    vec2 uv = vUV.st;
    vec4 current = texture(sTD2DInputs[0], uv);
    vec4 history = texture(sTD2DInputs[1], uv);
    float amount = clamp(interactionSmoothing, 0.0, 0.92);
    // Smoothness sets the overall temporal filtering. Response independently
    // controls how quickly new audience motion engages, while decay controls
    // how long the interaction tail remains after the audience moves away.
    // The defaults preserve the previously accepted attack/release behavior.
    float attackBlend = clamp(
        mix(1.0, 0.20, amount) *
        mix(0.35, 1.35, clamp(interactionResponse, 0.0, 1.0)),
        0.01, 1.0);
    float releaseBlend = clamp(
        mix(1.0, 0.55, amount) *
        mix(1.70, 0.30, clamp(interactionDecay, 0.0, 1.0)),
        0.01, 1.0);
    float blend = current.a >= history.a ? attackBlend : releaseBlend;
    vec3 force = mix(history.rgb, current.rgb, blend);
    float occupancy = mix(history.a, current.a, blend);
    fragColor = TDOutputSwizzle(vec4(force, occupancy));
}
''',
    "interaction_debug": r'''// CONTRACT: INTERACTION -> display-only signed force/occupancy color
out vec4 fragColor;

void main()
{
    vec4 interaction = texture(sTD2DInputs[0], vUV.st);
    float occupancy = clamp(interaction.a, 0.0, 1.0);
    float magnitude = length(interaction.rgb);
    float forceVisible = smoothstep(0.0001, 0.03, magnitude);
    float presence = clamp(max(occupancy, magnitude * 12.0), 0.0, 1.0);
    vec3 occupancyColor = vec3(0.05, 0.80, 1.00);
    vec3 directionColor = 0.5 + 0.5 *
        clamp(interaction.rgb * 12.0, vec3(-1.0), vec3(1.0));
    vec3 color = mix(occupancyColor, directionColor, 0.65 * forceVisible) *
                 presence;
    fragColor = TDOutputSwizzle(vec4(clamp(color, 0.0, 1.0), 1.0));
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
    "temporal_advect": r'''// CONTRACT: HISTORY + INTERACTION + FRAME_CONTROL -> BASE_HISTORY
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 history = texture(sTD2DInputs[0], uv);
    vec4 control = texture(sTD2DInputs[2], vec2(0.5));
    // Interaction is applied later in per-output view branches. Keeping this
    // history interaction-neutral lets disabled walls remain truly unchanged.
    float motionDt = min(max(control.g, 0.0), 1.0 / 15.0);
    float historyAlive = step(0.001, history.a);
    vec3 carried = history.rgb + vec3(0.0) * motionDt * historyAlive;
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
    vec2 q = uv * 2.0 - 1.0;
    float radius2 = dot(q, q);
    float shell = sqrt(max(0.0, 1.0 - min(radius2, 1.0)));
    float grain = hash21(floor(uv * 512.0));
    // Vary depth while unprojecting from the same source UV. This keeps the
    // procedural surface behind the missing pixel instead of sliding it into
    // a different screen region and opening a second, artificial black hole.
    float generatedDepth = 1.45 + shell * 0.85;
    const float generatedFocal = 0.86602540378; // 60-degree vertical FOV
    vec3 generated = vec3((uv.x - 0.5) * generatedDepth / generatedFocal,
                          (uv.y - 0.5) * generatedDepth / generatedFocal,
                          -generatedDepth);
    generated += (grain - 0.5) * vec3(0.035, 0.035, 0.12);
    // Invalid depth can occur anywhere in the rectangular source image,
    // especially in sky and reflective areas. Keep the backfill rectangular;
    // a circular activation mask creates a visible coloured dome in OUT_COLOR.
    float generatedActive = 1.0;
    float useMeasured = step(0.5, measured.a);
    vec3 position = mix(generated, measured.rgb, useMeasured);
    float activity = max(measured.a, generatedActive * (1.0 - measured.a));
    fragColor = TDOutputSwizzle(vec4(position, activity));
}
''',
    "view_interaction": r'''// CONTRACT: BASE_POSITION + INTERACTION -> VIEW_POSITION
out vec4 fragColor;

void main()
{
    const float viewInteractionGain = 0.0; // FLEXGPU_VIEW_INTERACTION_GAIN
    // A unit setting gives a noticeable but bounded displacement. The public
    // per-output intensity multiplies this 0.18-metre view-space response.
    const float viewInteractionMetres = 0.18;
    vec2 uv = vUV.st;
    // Avoid generic identifiers such as `interaction` and `active` here. They
    // collide with names emitted by TouchDesigner's generated GLSL wrapper and
    // can silently substitute a normalized color result for signed positions.
    vec4 positionSample = texture(sTD2DInputs[0], uv);
    vec4 interactionSample = texture(sTD2DInputs[1], uv);
    float positionMask = step(0.001, positionSample.a);
    vec3 displacedPosition = positionSample.rgb +
        interactionSample.rgb * viewInteractionGain * viewInteractionMetres *
        positionMask;
    fragColor = TDOutputSwizzle(vec4(displacedPosition, positionSample.a));
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
    // Preserve the generated image colour in invalid-depth regions. A subtle
    // luminance modulation still identifies procedural geometry without
    // painting a blue/pink overlay over the source image.
    vec3 palette = source.rgb * mix(0.88, 1.0, bands);
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

vec3 flexgpuHueRotate(vec3 color, float degrees)
{
    vec3 axis = normalize(vec3(1.0));
    float angle = radians(degrees);
    float cosine = cos(angle);
    float sine = sin(angle);
    return color * cosine + cross(axis, color) * sine +
           axis * dot(axis, color) * (1.0 - cosine);
}

vec3 flexgpuColorGrade(vec3 color)
{
    const float colorBrightness = 0.0; // FLEXGPU_COLOR_BRIGHTNESS
    const float colorContrast = 1.0; // FLEXGPU_COLOR_CONTRAST
    const float colorSaturation = 1.0; // FLEXGPU_COLOR_SATURATION
    const float colorGamma = 1.0; // FLEXGPU_COLOR_GAMMA
    const float colorHueShiftDegrees = 0.0; // FLEXGPU_COLOR_HUE_SHIFT
    const float colorTemperature = 0.0; // FLEXGPU_COLOR_TEMPERATURE
    const float colorTint = 0.0; // FLEXGPU_COLOR_TINT
    color = max(color, vec3(0.0));
    color = flexgpuHueRotate(color, colorHueShiftDegrees);
    color *= vec3(1.0 + colorTemperature * 0.18,
                  1.0,
                  1.0 - colorTemperature * 0.18);
    color *= vec3(1.0 + colorTint * 0.10,
                  1.0 - colorTint * 0.12,
                  1.0 + colorTint * 0.10);
    float luminance = dot(color, vec3(0.2126, 0.7152, 0.0722));
    color = mix(vec3(luminance), color, colorSaturation);
    color = (color - vec3(0.5)) * colorContrast + vec3(0.5);
    color += vec3(colorBrightness);
    color = pow(max(color, vec3(0.0)),
                vec3(1.0 / max(colorGamma, 0.05)));
    return clamp(color, 0.0, 1.0);
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
    // Never add the whole completion colour plate behind a camera render.
    // That produced a stretched, dark duplicate of the source in every
    // installation and triple-surface view. Use it only as the colour of
    // local disocclusion fog around actual point silhouettes.
    vec3 color = points.rgb + edgeColor * viewFog;
    float ambientFog = (1.0 - points.a) * grain *
                       clamp(viewFogDensity / 0.35, 0.0, 2.0) * 0.018;
    color += vec3(0.025, 0.045, 0.070) * ambientFog;
    color = color / (1.0 + color); // inexpensive tone map
    // Grade only actual point coverage. The delta form makes neutral defaults
    // an exact passthrough and leaves disocclusion fog/background unchanged.
    vec3 pointToneMapped = points.rgb / (1.0 + points.rgb);
    vec3 gradedPointColor = flexgpuColorGrade(pointToneMapped);
    color += (gradedPointColor - pointToneMapped) *
             clamp(points.a, 0.0, 1.0);
    color = clamp(color, 0.0, 1.0);
    color *= mix(0.84, 1.0, vignette);
    fragColor = TDOutputSwizzle(vec4(color, 1.0));
}
''',
    "panoramic_coverage": r'''// CONTRACT: WRAP POINT_RENDER -> procedural atmospheric coverage
out vec4 fragColor;

float hash21(vec2 p)
{
    p = fract(p * vec2(197.17, 431.39));
    p += dot(p, p + 37.23);
    return fract(p.x * p.y);
}

void main()
{
    const float wrapCoverage = 0.55; // FLEXGPU_WRAP_COVERAGE
    const float wrapNoise = 0.42; // FLEXGPU_WRAP_NOISE
    const float wrapPanelIndex = 1.0; // FLEXGPU_WRAP_PANEL_INDEX
    vec2 uv = vUV.st;
    vec4 points = texture(sTD2DInputs[0], uv);
    vec2 texel = 1.0 / vec2(textureSize(sTD2DInputs[0], 0));

    // A wider neighbourhood extends only the atmosphere surrounding real
    // points. It never reprojects or stretches the generated RGB image.
    float nearby = 0.0;
    for (int ring = 1; ring <= 4; ++ring) {
        vec2 d = texel * float(ring * 3);
        nearby = max(nearby, texture(sTD2DInputs[0], uv + vec2(d.x, 0.0)).a);
        nearby = max(nearby, texture(sTD2DInputs[0], uv - vec2(d.x, 0.0)).a);
        nearby = max(nearby, texture(sTD2DInputs[0], uv + vec2(0.0, d.y)).a);
        nearby = max(nearby, texture(sTD2DInputs[0], uv - vec2(0.0, d.y)).a);
    }

    // The panel index turns three local UV domains into one 3:1 panorama, so
    // low-frequency noise does not restart at every projector seam.
    vec2 panoramaUV = vec2((uv.x + wrapPanelIndex) / 3.0, uv.y);
    float coarse = hash21(floor(panoramaUV * vec2(180.0, 108.0)));
    float fine = hash21(floor(panoramaUV * vec2(720.0, 405.0)));
    float grain = mix(0.72, mix(coarse, fine, 0.35),
                      clamp(wrapNoise, 0.0, 1.0));
    float horizon = exp(-pow((uv.y - 0.46) * 2.7, 2.0));
    float upperMist = smoothstep(0.94, 0.45, uv.y);
    float empty = 1.0 - step(0.002, points.a);
    float localHaze = empty * nearby * (0.18 + 0.36 * grain);
    float proceduralMist = empty *
        (0.18 + 0.28 * horizon + 0.10 * upperMist) *
        (0.72 + 0.28 * grain);
    float coverage = clamp(max(localHaze, proceduralMist) *
                           max(0.0, wrapCoverage), 0.0, 0.55);

    // A low, continuous atmospheric floor keeps an empty side wall from
    // reading as a failed black projector. It is generated in panorama space
    // and remains intentionally abstract: no generated RGB pixels are copied,
    // mirrored, or stretched into the disoccluded region.
    float panoramaWave = 0.5 + 0.5 *
        sin((panoramaUV.x * 2.0 + panoramaUV.y * 0.35) * 6.2831853);
    float continuousFill = empty * clamp(wrapCoverage, 0.0, 1.0) *
        (0.30 + 0.42 * horizon + 0.12 * upperMist) *
        (0.76 + 0.24 * panoramaWave);
    float dust = empty * smoothstep(0.82, 0.98, fine) *
        (0.05 + 0.12 * horizon) * clamp(wrapNoise, 0.0, 1.0);
    vec3 coolMist = vec3(0.055, 0.065, 0.075);
    vec3 warmMist = vec3(0.180, 0.135, 0.095);
    vec3 mistColor = mix(coolMist, warmMist,
                         horizon * (0.38 + 0.32 * panoramaWave));
    vec3 color = points.rgb + mistColor *
        (coverage * 0.95 + continuousFill * 0.82 + dust);
    float alpha = max(points.a, max(coverage * 0.55,
                                   continuousFill * 0.45));
    fragColor = TDOutputSwizzle(vec4(color, alpha));
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

vec3 flexgpuHueRotate(vec3 color, float degrees)
{
    vec3 axis = normalize(vec3(1.0));
    float angle = radians(degrees);
    float cosine = cos(angle);
    float sine = sin(angle);
    return color * cosine + cross(axis, color) * sine +
           axis * dot(axis, color) * (1.0 - cosine);
}

vec3 flexgpuColorGrade(vec3 color)
{
    const float colorBrightness = 0.0; // FLEXGPU_COLOR_BRIGHTNESS
    const float colorContrast = 1.0; // FLEXGPU_COLOR_CONTRAST
    const float colorSaturation = 1.0; // FLEXGPU_COLOR_SATURATION
    const float colorGamma = 1.0; // FLEXGPU_COLOR_GAMMA
    const float colorHueShiftDegrees = 0.0; // FLEXGPU_COLOR_HUE_SHIFT
    const float colorTemperature = 0.0; // FLEXGPU_COLOR_TEMPERATURE
    const float colorTint = 0.0; // FLEXGPU_COLOR_TINT
    color = max(color, vec3(0.0));
    color = flexgpuHueRotate(color, colorHueShiftDegrees);
    color *= vec3(1.0 + colorTemperature * 0.18,
                  1.0,
                  1.0 - colorTemperature * 0.18);
    color *= vec3(1.0 + colorTint * 0.10,
                  1.0 - colorTint * 0.12,
                  1.0 + colorTint * 0.10);
    float luminance = dot(color, vec3(0.2126, 0.7152, 0.0722));
    color = mix(vec3(luminance), color, colorSaturation);
    color = (color - vec3(0.5)) * colorContrast + vec3(0.5);
    color += vec3(colorBrightness);
    color = pow(max(color, vec3(0.0)),
                vec3(1.0 / max(colorGamma, 0.05)));
    return clamp(color, 0.0, 1.0);
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
    // Grade only actual point coverage. The delta form makes neutral defaults
    // an exact passthrough and leaves disocclusion fog/background unchanged.
    vec3 pointToneMapped = points.rgb / (1.0 + points.rgb);
    vec3 gradedPointColor = flexgpuColorGrade(pointToneMapped);
    color += (gradedPointColor - pointToneMapped) *
             clamp(points.a, 0.0, 1.0);
    color = clamp(color, 0.0, 1.0);
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
    // Constant TOPs premultiply RGB by alpha. These 1x1 TOPs carry data,
    // so recover their authored RGB values before interpreting the channels.
    vec4 depthCalibrationPacked = texelFetch(sTD2DInputs[1], ivec2(0, 0), 0);
    vec4 intrinsicsPacked = texelFetch(sTD2DInputs[2], ivec2(0, 0), 0);
    vec4 depthCalibration = vec4(
        depthCalibrationPacked.rgb / max(abs(depthCalibrationPacked.a), 1e-6),
        depthCalibrationPacked.a);
    vec4 normalizedIntrinsics = vec4(
        intrinsicsPacked.rgb / max(abs(intrinsicsPacked.a), 1e-6),
        intrinsicsPacked.a);
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


SHOW_CONTROL_CALLBACKS = r'''# Parameter Execute DAT callbacks for public show controls.
import math
import os
import re
import subprocess

_WORKER_PROCESSES = {}

_FLOAT_PATTERN = r'[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?'

def _controls():
    return parent()

def _pipeline():
    return parent().parent()

def _set(node, name, value):
    if node is None:
        return False
    try:
        getattr(node.par, name).val = value
        return True
    except Exception:
        try:
            wanted = str(name).lower()
            for parameter in node.pars():
                if str(parameter.name).lower() == wanted:
                    parameter.val = value
                    return True
        except Exception:
            pass
    return False

def _value(name, fallback=None):
    controls = _controls()
    try:
        return getattr(controls.par, name).eval()
    except Exception:
        try:
            wanted = str(name).lower()
            for parameter in controls.pars():
                if str(parameter.name).lower() == wanted:
                    return parameter.eval()
        except Exception:
            pass
    return fallback

def _patch_float(dat, symbol, marker, value):
    if dat is None:
        return False
    pattern = (
        r'(const\s+float\s+' + re.escape(symbol) + r'\s*=\s*)' +
        _FLOAT_PATTERN + r'(\s*;\s*//\s*' + re.escape(marker) + r')')
    replacement = r'\g<1>' + ('%.6g' % float(value)) + r'\g<2>'
    updated, count = re.subn(pattern, replacement, str(dat.text), count=1)
    if count == 1:
        dat.text = updated
        return True
    return False

def _patch_vec4(dat, symbol, marker, values):
    if dat is None:
        return False
    try:
        numbers = [float(value) for value in values]
    except Exception:
        return False
    if len(numbers) != 4 or not all(math.isfinite(value) for value in numbers):
        return False
    pattern = (
        r'(const\s+vec4\s+' + re.escape(symbol) +
        r'\s*=\s*vec4\()[^)]*(\)\s*;\s*//\s*' +
        re.escape(marker) + r')')
    replacement = (
        r'\g<1>' +
        ', '.join('%.9g' % value for value in numbers) +
        r'\g<2>')
    updated, count = re.subn(pattern, replacement, str(dat.text), count=1)
    if count == 1:
        dat.text = updated
        return True
    return False

def _set_resolution(node, width, height):
    if node is None:
        return
    _set(node, 'outputresolution', 'custom')
    _set(node, 'resmult', False)
    _set(node, 'resolutionw', int(width))
    _set(node, 'resolutionh', int(height))
    _set(node, 'outputaspect', 'resolution')

def _geometry_contract_dimensions():
    pipeline = _pipeline()
    reconstruction = pipeline.op('RECONSTRUCTION')
    geometry = max(
        64, min(2048, int(_value('Geometryresolution', 384))))
    preserve = bool(_value('Preservegeometryaspect', True))
    aspect = 16.0 / 9.0
    rgb = (
        reconstruction.op('RGB_IN')
        if reconstruction is not None else None)
    try:
        if rgb is not None and rgb.width > 0 and rgb.height > 0:
            aspect = max(1.0 / 16.0, min(
                16.0, float(rgb.width) / float(rgb.height)))
    except Exception:
        pass
    if not preserve:
        return geometry, geometry
    width = max(
        64, min(
            2048,
            2 * int(round((geometry * aspect ** 0.5) / 2.0))))
    height = max(
        64, min(
            2048,
            2 * int(round((geometry / aspect ** 0.5) / 2.0))))
    return width, height

def _apply_geometry_contract_resolution():
    pipeline = _pipeline()
    geometry = max(
        64, min(2048, int(_value('Geometryresolution', 384))))
    preserve = bool(_value('Preservegeometryaspect', True))
    _set(pipeline.op('RECONSTRUCTION'), 'Geometryresolution', geometry)
    _set(pipeline.op('RECONSTRUCTION'), 'Preservegeometryaspect', preserve)
    _set(pipeline.parent().op('AI_PIPELINE'), 'Geometryresolution', geometry)
    width, height = _geometry_contract_dimensions()
    # The interaction field intentionally stays at its small sensor budget.
    # Force only the shaders that combine it with the generated position
    # texture back to the dense geometry contract. Otherwise TouchDesigner's
    # common-input resolution can collapse a 682x384 position field to the
    # 128x128 interaction texture and leave only 16,384 source points.
    for path in (
        'COMPLETION/INTERACTION_RENDER_RESIZE',
        'COMPLETION/procedural_backfill',
        'POINT_RENDER/INTERACTION_RENDER_RESIZE',
        'POINT_RENDER/VIEW_POSITION_INSTALLATION',
        'POINT_RENDER/VIEW_POSITION_LEFT',
        'POINT_RENDER/VIEW_POSITION_CENTER',
        'POINT_RENDER/VIEW_POSITION_RIGHT',
    ):
        _set_resolution(pipeline.op(path), width, height)

def _set_horizontal_layout(node):
    if node is None:
        return
    try:
        if getattr(node.par, 'align', None) is not None:
            _set(node, 'align', 'horizlr')
        else:
            _set(node, 'direction', 'horizontal')
    except Exception:
        pass

def _apply_wall_resolution():
    pipeline = _pipeline()
    width = max(320, min(3840, int(_value('Wallwidth', 1920))))
    height = max(180, min(2160, int(_value('Wallheight', 1080))))
    controls = _controls()
    _set(controls, 'Wallwidth', width)
    _set(controls, 'Wallheight', height)
    for path in (
        'POINT_RENDER/METRIC_RENDER_CENTER',
        'POINT_RENDER/METRIC_MONO_FALLBACK',
        'INSTALLATION_OUTPUT/installation_grade',
    ):
        _set_resolution(pipeline.op(path), width, height)
    for mode in ('WRAP', 'ARTISTIC'):
        for side in ('LEFT', 'CENTER', 'RIGHT'):
            _set_resolution(
                pipeline.op('POINT_RENDER/METRIC_RENDER_%s_%s' %
                            (mode, side)), width, height)
            if mode == 'WRAP':
                _set_resolution(
                    pipeline.op('TRIPLE_DISPLAY/COVERAGE_WRAP_' + side),
                    width, height)
            _set_resolution(
                pipeline.op('TRIPLE_DISPLAY/GRADE_%s_%s' % (mode, side)),
                width, height)
        for suffix in ('', '_FALLBACK'):
            mosaic = pipeline.op(
                'TRIPLE_DISPLAY/%s_MOSAIC%s' % (mode, suffix))
            _set_resolution(mosaic, width * 3, height)
            _set_horizontal_layout(mosaic)
    try:
        pipeline.store(
            'venue_output_profile',
            'custom_%dx%d' % (width, height))
    except Exception:
        pass

def _apply_point_cloud_scale():
    pipeline = _pipeline()
    provider = str(_value('Geometryprovider', 'moge2')).strip().lower()
    creative = max(
        0.5, min(2.5, float(_value('Pointcloudscale', 1.0))))
    provider_name = (
        'Depthanythingscale' if provider == 'depth_anything'
        else 'Moge2scale')
    provider_scale = max(
        0.5, min(2.5, float(_value(provider_name, 1.0))))
    effective = max(0.35, min(4.0, creative * provider_scale))
    _set(pipeline.op('POINT_RENDER'), 'Pointcloudscale', effective)
    _set(_controls(), 'Effectivepointcloudscale', effective)

def _apply_artistic_offset_direction():
    controls = _controls()
    pipeline = _pipeline()
    direction = str(
        _value('Artisticoffsetdirection', 'outward')).strip().lower()
    if direction not in ('outward', 'inward'):
        direction = 'outward'
    _set(pipeline.op('POINT_RENDER'), 'Artisticoffsetdirection', direction)
    _set(controls, 'Artisticoffsetdirection', direction)

def _apply_wall_view_control(name):
    key = str(name).lower()
    scale_parameters = {
        'leftwallscale': 'Leftwallscale',
        'centerwallscale': 'Centerwallscale',
        'rightwallscale': 'Rightwallscale',
    }
    pan_parameters = {
        'leftwallpanhorizontaldegrees': 'Leftwallpanhorizontaldegrees',
        'leftwallpanverticaldegrees': 'Leftwallpanverticaldegrees',
        'centerwallpanhorizontaldegrees': 'Centerwallpanhorizontaldegrees',
        'centerwallpanverticaldegrees': 'Centerwallpanverticaldegrees',
        'rightwallpanhorizontaldegrees': 'Rightwallpanhorizontaldegrees',
        'rightwallpanverticaldegrees': 'Rightwallpanverticaldegrees',
    }
    parameter = scale_parameters.get(key) or pan_parameters.get(key)
    if parameter is None:
        return
    fallback, lower, upper = (
        (1.0, 0.25, 4.0) if key in scale_parameters
        else (0.0, -89.0, 89.0))
    value = max(
        lower, min(upper, float(_value(parameter, fallback))))
    _set(_pipeline().op('POINT_RENDER'), parameter, value)
    _set(_controls(), parameter, value)

def _apply_audio_controls():
    controls = _controls()
    pipeline = _pipeline()
    adapter = pipeline.op('SOURCES/STREAMDIFFUSION_ADAPTER')
    enabled = bool(_value('Audioenabled', False))
    source = str(_value('Audiosource', 'voices')).strip().lower()
    if source not in ('voices', 'soundscape'):
        source = 'voices'
    _set(controls, 'Audioenabled', enabled)
    _set(controls, 'Audiosource', source)
    _set(adapter, 'Audioenabled', enabled)
    _set(adapter, 'Audiosource', source)
    if adapter is None:
        return
    adapter_control = adapter.op('show_control')
    _set(adapter_control, 'Audioenabled', enabled)
    _set(adapter_control, 'Audiosource', source)
    audio_switch = adapter.op('audiosource_switch')
    _set(audio_switch, 'index', 1 if source == 'soundscape' else 0)
    audio_out = adapter.op('audio_out')
    if audio_out is not None:
        try:
            audio_out.par.active.expr = 'parent().par.Audioenabled'
            audio_out.par.active.mode = ParMode.EXPRESSION
        except Exception:
            _set(audio_out, 'active', enabled)

def _select_femto_device(femto, requested_serial):
    primary = femto.op('FEMTO_PRIMARY') if femto is not None else None
    if primary is None:
        return False, '', 'Femto Mega unavailable: Orbbec TOP is missing'
    try:
        choices = [str(item) for item in primary.par.device.menuNames]
    except Exception:
        choices = []
    serial = str(requested_serial or '').strip()
    selected = ''
    if serial:
        selected = next(
            (item for item in choices if serial.lower() in item.lower()), '')
        if not selected:
            return (
                False, serial,
                'Femto Mega unavailable: serial %s not found' % serial)
    elif choices:
        selected = choices[0]
    if not selected:
        return False, serial, 'Femto Mega unavailable: no USB device detected'
    try:
        primary.par.device.val = selected
    except Exception as exc:
        return False, serial, 'Femto Mega device selection failed: %s' % exc
    resolved = serial
    if not resolved:
        parts = selected.split('|||')
        resolved = parts[-2].strip() if len(parts) >= 2 else selected
    _set(femto, 'Deviceserial', serial)
    return True, resolved, ''

def _apply_camera_interaction():
    controls = _controls()
    pipeline = _pipeline()
    sensor = pipeline.op('SENSOR_INTERACTION')
    adapter = sensor.op('DEPTH_SENSOR_ADAPTER') if sensor is not None else None
    bridge = (
        adapter.op('DEPTH_ANYTHING_BRIDGE')
        if adapter is not None else None)
    femto = pipeline.op('SOURCES/FEMTO_MEGA_ADAPTER')
    enabled = bool(_value('Camerainteractionenabled', False))
    mirrored = bool(_value('Cameramirrorhorizontal', True))
    source = str(
        _value('Camerasensorsource', 'depth_anything')).strip().lower()
    if source not in ('depth_anything', 'femto_mega'):
        source = 'depth_anything'
    femto_serial = str(_value('Femtodeviceserial', '') or '').strip()
    _set(controls, 'Camerainteractionenabled', enabled)
    _set(controls, 'Cameramirrorhorizontal', mirrored)
    _set(controls, 'Camerasensorsource', source)
    _set(sensor, 'Mode', 'depth_sensor' if enabled else 'disabled')
    _set(adapter, 'Sensorsource', source)
    _set(adapter, 'Enabled', enabled)
    _set(bridge, 'Mirrorhorizontal', mirrored)
    femto_enabled = False
    status = 'inactive; webcam + Depth Anything settings are preserved'
    if source == 'femto_mega':
        device_ready, resolved_serial, detail = _select_femto_device(
            femto, femto_serial)
        femto_enabled = bool(enabled and device_ready)
        if detail:
            status = detail
        elif not enabled:
            status = 'Femto Mega selected but camera interaction is disabled'
        else:
            try:
                result_valid = bool(femto.par.Resultvalid.eval())
            except Exception:
                result_valid = False
            status = (
                ('ready: ' if result_valid else 'starting: ') +
                (resolved_serial or 'auto-selected USB device'))
    _set(femto, 'Enabled', femto_enabled)
    _set(controls, 'Femtostatus', status)
    if enabled and source == 'depth_anything' and bridge is not None:
        runtime_dat = bridge.op('sensor_runtime')
        if runtime_dat is not None:
            try:
                runtime_dat.module.tick(bridge)
            except Exception:
                pass

def _apply_sensor_calibration_trim():
    controls = _controls()
    sensor = _pipeline().op('SENSOR_INTERACTION')
    shader = (
        sensor.op('CALIBRATE_SENSOR_POSITION_PIXEL')
        if sensor is not None else None)
    if sensor is None or shader is None:
        return
    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    for index, fallback in enumerate(identity):
        try:
            raw = getattr(
                sensor.par, 'Sensortoworld%d' % index).eval()
            values = [
                float(value)
                for value in str(raw).replace(',', ' ').split()]
        except Exception:
            values = list(fallback)
        if (len(values) != 4 or
                not all(math.isfinite(value) for value in values)):
            values = list(fallback)
        _patch_vec4(
            shader, 'sensorToWorld%d' % index,
            'FLEXGPU_SENSOR_TO_WORLD_%d' % index, values)
    source = str(
        _value('Camerasensorsource', 'depth_anything')).strip().lower()
    prefix = 'Femto' if source == 'femto_mega' else 'Sensor'
    settings = (
        (prefix + 'positionscale', 'sensorPositionScale',
         'FLEXGPU_SENSOR_POSITION_SCALE', 1.0, 0.25, 4.0),
        (prefix + 'trimxmetres', 'sensorTrimXMetres',
         'FLEXGPU_SENSOR_TRIM_X', 0.0, -5.0, 5.0),
        (prefix + 'trimymetres', 'sensorTrimYMetres',
         'FLEXGPU_SENSOR_TRIM_Y', 0.0, -5.0, 5.0),
        (prefix + 'trimzmetres', 'sensorTrimZMetres',
         'FLEXGPU_SENSOR_TRIM_Z', 0.0, -5.0, 5.0),
        (prefix + 'trimyawdegrees', 'sensorTrimYawDegrees',
         'FLEXGPU_SENSOR_TRIM_YAW', 0.0, -180.0, 180.0),
        (prefix + 'trimpitchdegrees', 'sensorTrimPitchDegrees',
         'FLEXGPU_SENSOR_TRIM_PITCH', 0.0, -90.0, 90.0),
        (prefix + 'trimrolldegrees', 'sensorTrimRollDegrees',
         'FLEXGPU_SENSOR_TRIM_ROLL', 0.0, -180.0, 180.0),
    )
    for parameter, symbol, marker, fallback, lower, upper in settings:
        value = max(
            lower, min(upper, float(_value(parameter, fallback))))
        _set(controls, parameter, value)
        _patch_float(shader, symbol, marker, value)

def _reset_sensor_calibration_trim(requested_prefix=None):
    controls = _controls()
    source = str(
        _value('Camerasensorsource', 'depth_anything')).strip().lower()
    prefix = (
        requested_prefix
        if requested_prefix in ('Sensor', 'Femto')
        else ('Femto' if source == 'femto_mega' else 'Sensor'))
    for parameter, value in (
            ('positionscale', 1.0),
            ('trimxmetres', 0.0),
            ('trimymetres', 0.0),
            ('trimzmetres', 0.0),
            ('trimyawdegrees', 0.0),
            ('trimpitchdegrees', 0.0),
            ('trimrolldegrees', 0.0)):
        _set(controls, prefix + parameter, value)
    _apply_sensor_calibration_trim()

def _apply_femto_depth_gate():
    controls = _controls()
    femto = _pipeline().op('SOURCES/FEMTO_MEGA_ADAPTER')
    validity_shader = (
        femto.op('DERIVE_SENSOR_VALIDITY_PIXEL')
        if femto is not None else None)
    position_shader = (
        femto.op('CONVERT_SENSOR_POSITION_PIXEL')
        if femto is not None else None)
    if femto is None or validity_shader is None or position_shader is None:
        return
    mirrored = bool(_value('Femtomirrorhorizontal', False))
    near_metres = max(
        0.10, min(15.0, float(_value('Femtoaudiencenearmetres', 0.25))))
    far_metres = max(
        near_metres + 0.10,
        min(20.0, float(_value('Femtoaudiencefarmetres', 12.0))))
    _set(controls, 'Femtomirrorhorizontal', mirrored)
    _set(controls, 'Femtoaudiencenearmetres', near_metres)
    _set(controls, 'Femtoaudiencefarmetres', far_metres)
    _patch_float(
        position_shader, 'femtoMirrorHorizontal',
        'FLEXGPU_FEMTO_MIRROR_HORIZONTAL', 1.0 if mirrored else 0.0)
    _patch_float(
        validity_shader, 'femtoNearMetres',
        'FLEXGPU_FEMTO_NEAR_METRES', near_metres)
    _patch_float(
        validity_shader, 'femtoFarMetres',
        'FLEXGPU_FEMTO_FAR_METRES', far_metres)

def _workspace_root():
    configured = str(_value('Workspaceroot', '') or '').strip()
    candidates = []
    if configured:
        candidates.append(configured)
    try:
        candidates.extend((project.folder, os.path.dirname(project.folder)))
    except Exception:
        pass
    for candidate in candidates:
        root = os.path.abspath(os.path.expanduser(str(candidate)))
        if (os.path.isfile(os.path.join(
                root, 'scripts', 'Start-MoGe2Worker.ps1')) and
                os.path.isfile(os.path.join(
                    root, 'scripts',
                    'Start-DepthAnythingGeometryWorker.ps1'))):
            return root
    return ''

def _launch_worker(provider):
    controls = _controls()
    selected = str(provider).strip().lower()
    previous = _WORKER_PROCESSES.get(selected)
    if previous is not None and previous.poll() is None:
        _set(controls, 'Workerstatus',
             '%s worker is already running (PID %s)' %
             (selected, previous.pid))
        _set(controls, 'Workerpid', int(previous.pid))
        return False
    root = _workspace_root()
    if not root:
        _set(controls, 'Workerstatus',
             'Worker start failed: set Workspace Root to this checkout')
        return False
    script_name = (
        'Start-DepthAnythingGeometryWorker.ps1'
        if selected == 'depth_anything' else 'Start-MoGe2Worker.ps1')
    script = os.path.abspath(os.path.join(root, 'scripts', script_name))
    scripts_root = os.path.abspath(os.path.join(root, 'scripts'))
    try:
        if os.path.commonpath((script, scripts_root)) != scripts_root:
            raise ValueError('worker script escaped the workspace')
    except Exception as exc:
        _set(controls, 'Workerstatus', 'Worker start refused: %s' % exc)
        return False
    profile = str(_value('Qualityprofile', '3080ti_16gb'))
    if profile not in ('3080ti_16gb', '4090', '5090'):
        profile = '3080ti_16gb'
    gpu_index = max(0, min(31, int(_value('Gpuindex', 0))))
    _set(controls, 'Geometryprovider', selected)
    apply_parameter('Geometryprovider')
    # Keep the console visible while the foreground worker is alive, but let
    # PowerShell exit with the worker. ``-NoExit`` leaves an empty wrapper
    # process behind after a console interrupt or worker failure, causing the duplicate
    # launch guard above to report a worker that no longer exists.
    args = [
        'powershell.exe', '-NoProfile',
        '-ExecutionPolicy', 'Bypass', '-File', script,
        '-Profile', profile, '-Backend', selected,
        '-GpuIndex', str(gpu_index), '-Start',
    ]
    try:
        process = subprocess.Popen(
            args, cwd=root,
            creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
        _WORKER_PROCESSES[selected] = process
        _set(controls, 'Workspaceroot', root)
        _set(controls, 'Workerpid', int(process.pid))
        _set(controls, 'Workerstatus',
             '%s worker console opened (PID %s); use its Stop button' %
             (selected, process.pid))
        return True
    except Exception as exc:
        _set(controls, 'Workerpid', 0)
        _set(controls, 'Workerstatus',
             'Worker start failed: %s' % str(exc)[:240])
        return False

def _stop_worker(provider):
    controls = _controls()
    selected = str(provider).strip().lower()
    if selected not in ('moge2', 'depth_anything'):
        _set(controls, 'Workerstatus',
             'Worker stop refused: unsupported provider %s' % selected)
        return False
    root = _workspace_root()
    if not root:
        _set(controls, 'Workerstatus',
             'Worker stop failed: set Workspace Root to this checkout')
        return False
    script = os.path.abspath(os.path.join(
        root, 'scripts', 'Stop-GeneratedGeometryWorker.ps1'))
    scripts_root = os.path.abspath(os.path.join(root, 'scripts'))
    try:
        if (os.path.commonpath((script, scripts_root)) != scripts_root or
                not os.path.isfile(script)):
            raise ValueError('stop script is outside or missing from workspace')
    except Exception as exc:
        _set(controls, 'Workerstatus', 'Worker stop refused: %s' % exc)
        return False
    _set(controls, 'Workerstatus', 'Stopping %s worker...' % selected)
    args = [
        'powershell.exe', '-NoProfile',
        '-ExecutionPolicy', 'Bypass', '-File', script,
        '-Provider', selected, '-Stop',
    ]
    try:
        completed = subprocess.run(
            args, cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        _set(controls, 'Workerstatus',
             '%s worker stop timed out after 20 seconds' % selected)
        return False
    except Exception as exc:
        _set(controls, 'Workerstatus',
             'Worker stop failed: %s' % str(exc)[:240])
        return False
    if completed.returncode != 0:
        detail = ' '.join(str(completed.stdout or '').split())
        if not detail:
            detail = 'PowerShell returned %s' % completed.returncode
        _set(controls, 'Workerstatus',
             '%s worker stop failed: %s' % (selected, detail[-240:]))
        return False
    previous = _WORKER_PROCESSES.pop(selected, None)
    if previous is not None and previous.poll() is None:
        try:
            previous.wait(timeout=5)
        except Exception:
            try:
                previous.terminate()
                previous.wait(timeout=2)
            except Exception:
                pass
    output = str(completed.stdout or '')
    _set(controls, 'Workerpid', 0)
    if 'No matching worker is running' in output:
        _set(controls, 'Workerstatus',
             'No %s worker is running' % selected)
    else:
        _set(controls, 'Workerstatus',
             '%s worker stopped; its console should close' % selected)
    return True

def _launch_sensor_worker():
    controls = _controls()
    previous = _WORKER_PROCESSES.get('sensor')
    if previous is not None and previous.poll() is None:
        _set(controls, 'Sensorworkerstatus',
             'camera depth worker is already running (PID %s)' %
             previous.pid)
        _set(controls, 'Sensorworkerpid', int(previous.pid))
        return False
    root = _workspace_root()
    if not root:
        _set(controls, 'Sensorworkerstatus',
             'Camera start failed: set Workspace Root to this checkout')
        return False
    script = os.path.abspath(os.path.join(
        root, 'scripts', 'Start-DepthAnythingWorker.ps1'))
    scripts_root = os.path.abspath(os.path.join(root, 'scripts'))
    try:
        if (os.path.commonpath((script, scripts_root)) != scripts_root or
                not os.path.isfile(script)):
            raise ValueError('camera worker script is outside or missing')
    except Exception as exc:
        _set(controls, 'Sensorworkerstatus',
             'Camera start refused: %s' % exc)
        return False
    profile = str(_value('Qualityprofile', '3080ti_16gb'))
    if profile not in ('3080ti_16gb', '4090', '5090'):
        profile = '3080ti_16gb'
    gpu_index = max(0, min(31, int(_value('Gpuindex', 0))))
    camera_index = max(0, min(31, int(_value('Cameraindex', 0))))
    camera_name = str(_value('Cameraname', '') or '').strip()
    if (len(camera_name.encode('utf-8')) > 255 or
            any(ord(character) < 32 for character in camera_name)):
        _set(controls, 'Sensorworkerstatus',
             'Camera start refused: invalid camera name')
        return False
    _set(controls, 'Camerasensorsource', 'depth_anything')
    _set(controls, 'Camerainteractionenabled', True)
    _apply_camera_interaction()
    args = [
        'powershell.exe', '-NoProfile',
        '-ExecutionPolicy', 'Bypass', '-File', script,
        '-Profile', profile, '-GpuIndex', str(gpu_index),
        '-CameraIndex', str(camera_index),
    ]
    if camera_name:
        args.extend(('-CameraName', camera_name))
    args.append('-Start')
    try:
        process = subprocess.Popen(
            args, cwd=root, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _WORKER_PROCESSES['sensor'] = process
        _set(controls, 'Workspaceroot', root)
        _set(controls, 'Sensorworkerpid', int(process.pid))
        _set(controls, 'Sensorworkerstatus',
             'camera depth worker started hidden (PID %s)' % process.pid)
        return True
    except Exception as exc:
        _set(controls, 'Sensorworkerpid', 0)
        _set(controls, 'Sensorworkerstatus',
             'Camera start failed: %s' % str(exc)[:240])
        return False

def _stop_sensor_worker():
    controls = _controls()
    root = _workspace_root()
    if not root:
        _set(controls, 'Sensorworkerstatus',
             'Camera stop failed: set Workspace Root to this checkout')
        return False
    script = os.path.abspath(os.path.join(
        root, 'scripts', 'Stop-DepthAnythingSensorWorker.ps1'))
    scripts_root = os.path.abspath(os.path.join(root, 'scripts'))
    try:
        if (os.path.commonpath((script, scripts_root)) != scripts_root or
                not os.path.isfile(script)):
            raise ValueError('camera stop script is outside or missing')
    except Exception as exc:
        _set(controls, 'Sensorworkerstatus',
             'Camera stop refused: %s' % exc)
        return False
    _set(controls, 'Sensorworkerstatus', 'Stopping camera depth worker...')
    args = [
        'powershell.exe', '-NoProfile',
        '-ExecutionPolicy', 'Bypass', '-File', script, '-Stop',
    ]
    try:
        completed = subprocess.run(
            args, cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        _set(controls, 'Sensorworkerstatus',
             'Camera depth worker stop timed out after 20 seconds')
        return False
    except Exception as exc:
        _set(controls, 'Sensorworkerstatus',
             'Camera stop failed: %s' % str(exc)[:240])
        return False
    if completed.returncode != 0:
        detail = ' '.join(str(completed.stdout or '').split())
        if not detail:
            detail = 'PowerShell returned %s' % completed.returncode
        _set(controls, 'Sensorworkerstatus',
             'Camera stop failed: %s' % detail[-240:])
        return False
    previous = _WORKER_PROCESSES.pop('sensor', None)
    if previous is not None and previous.poll() is None:
        try:
            previous.wait(timeout=5)
        except Exception:
            try:
                previous.terminate()
                previous.wait(timeout=2)
            except Exception:
                pass
    output = str(completed.stdout or '')
    _set(controls, 'Sensorworkerpid', 0)
    if 'No matching audience-camera worker is running' in output:
        _set(controls, 'Sensorworkerstatus',
             'No camera depth worker is running')
    else:
        _set(controls, 'Sensorworkerstatus',
             'Camera depth worker stopped; interaction will fail closed')
    return True

def _apply_fog():
    pipeline = _pipeline()
    density = max(0.0, min(1.5, float(_value('Fogdensity', 0.35))))
    _set(pipeline.op('COMPLETION'), 'Fogdensity', density)
    _set(pipeline.op('INSTALLATION_OUTPUT'), 'Fogdensity', density)
    _set(pipeline.op('TRIPLE_DISPLAY'), 'Fogdensity', density)
    _set(pipeline.op('STEREO_PREVIEW'), 'Fogdensity', density)
    _patch_float(pipeline.op('COMPLETION/fog_completion_PIXEL'),
                 'fogDensity', 'FLEXGPU_FOG_DENSITY', density)
    _patch_float(pipeline.op('INSTALLATION_OUTPUT/installation_grade_PIXEL'),
                 'viewFogDensity', 'FLEXGPU_VIEW_FOG_DENSITY', density)
    for mode in ('WRAP', 'ARTISTIC'):
        for side in ('LEFT', 'CENTER', 'RIGHT'):
            _patch_float(
                pipeline.op('TRIPLE_DISPLAY/GRADE_%s_%s_PIXEL' % (mode, side)),
                'viewFogDensity', 'FLEXGPU_VIEW_FOG_DENSITY', density)
    for eye in ('LEFT', 'RIGHT'):
        _patch_float(
            pipeline.op('STEREO_PREVIEW/GRADE_%s_EYE_PIXEL' % eye),
            'viewFogDensity', 'FLEXGPU_VIEW_FOG_DENSITY', density)

def _color_grade_dats():
    pipeline = _pipeline()
    result = [
        pipeline.op('INSTALLATION_OUTPUT/installation_grade_PIXEL'),
    ]
    for mode in ('WRAP', 'ARTISTIC'):
        for side in ('LEFT', 'CENTER', 'RIGHT'):
            result.append(pipeline.op(
                'TRIPLE_DISPLAY/GRADE_%s_%s_PIXEL' % (mode, side)))
    for eye in ('LEFT', 'RIGHT'):
        result.append(pipeline.op(
            'STEREO_PREVIEW/GRADE_%s_EYE_PIXEL' % eye))
    return result

def _apply_color_grade():
    controls = _controls()
    settings = (
        ('Brightness', 'colorBrightness',
         'FLEXGPU_COLOR_BRIGHTNESS', 0.0, -1.0, 1.0),
        ('Contrast', 'colorContrast',
         'FLEXGPU_COLOR_CONTRAST', 1.0, 0.0, 3.0),
        ('Saturation', 'colorSaturation',
         'FLEXGPU_COLOR_SATURATION', 1.0, 0.0, 3.0),
        ('Gamma', 'colorGamma',
         'FLEXGPU_COLOR_GAMMA', 1.0, 0.2, 3.0),
        ('Hueshiftdegrees', 'colorHueShiftDegrees',
         'FLEXGPU_COLOR_HUE_SHIFT', 0.0, -180.0, 180.0),
        ('Temperature', 'colorTemperature',
         'FLEXGPU_COLOR_TEMPERATURE', 0.0, -1.0, 1.0),
        ('Tint', 'colorTint',
         'FLEXGPU_COLOR_TINT', 0.0, -1.0, 1.0),
    )
    values = []
    for parameter, symbol, marker, fallback, lower, upper in settings:
        value = max(lower, min(
            upper, float(_value(parameter, fallback))))
        _set(controls, parameter, value)
        values.append((symbol, marker, value))
    for dat in _color_grade_dats():
        for symbol, marker, value in values:
            _patch_float(dat, symbol, marker, value)

def _reset_color_grade():
    controls = _controls()
    for parameter, value in (
            ('Brightness', 0.0),
            ('Contrast', 1.0),
            ('Saturation', 1.0),
            ('Gamma', 1.0),
            ('Hueshiftdegrees', 0.0),
            ('Temperature', 0.0),
            ('Tint', 0.0)):
        _set(controls, parameter, value)
    _apply_color_grade()

def _apply_output_interaction():
    controls = _controls()
    render = _pipeline().op('POINT_RENDER')
    if render is None:
        return
    for view, title, enabled_default in (
            ('INSTALLATION', 'Installation', True),
            ('LEFT', 'Leftwall', False),
            ('CENTER', 'Centerwall', True),
            ('RIGHT', 'Rightwall', False)):
        enabled_name = title + 'interactionenabled'
        intensity_name = title + 'interactionintensity'
        enabled = bool(_value(enabled_name, enabled_default))
        intensity = max(
            0.0, min(10.0, float(_value(intensity_name, 1.0))))
        _set(controls, enabled_name, enabled)
        _set(controls, intensity_name, intensity)
        _set(render, enabled_name, enabled)
        _set(render, intensity_name, intensity)
        _patch_float(
            render.op('VIEW_POSITION_%s_PIXEL' % view),
            'viewInteractionGain',
            'FLEXGPU_VIEW_INTERACTION_GAIN',
            intensity if enabled else 0.0)

def _apply_interaction_shape():
    controls = _controls()
    sensor = _pipeline().op('SENSOR_INTERACTION')
    if sensor is None:
        return
    radius = max(
        0.05, min(3.0, float(_value('Interactionradius', 0.55))))
    falloff = max(
        0.25, min(4.0, float(_value('Interactionfalloff', 1.0))))
    strength = max(
        0.0, min(2.0, float(_value('Interactionstrength', 0.35))))
    smoothing = max(
        0.0, min(0.92, float(_value('Interactionsmoothing', 0.35))))
    response = max(
        0.0, min(1.0, float(_value('Interactionresponse', 0.65))))
    decay = max(
        0.0, min(1.0, float(_value('Interactiondecay', 0.5))))
    for control_name, sensor_name, value in (
            ('Interactionradius', 'Interactionradius', radius),
            ('Interactionfalloff', 'Interactionfalloff', falloff),
            ('Interactionstrength', 'Forcegain', strength),
            ('Interactionsmoothing', 'Interactionsmoothing', smoothing),
            ('Interactionresponse', 'Interactionresponse', response),
            ('Interactiondecay', 'Interactiondecay', decay)):
        _set(controls, control_name, value)
        _set(sensor, sensor_name, value)
    _patch_float(
        sensor.op('interaction_field_PIXEL'),
        'interactionRadiusMetres', 'FLEXGPU_INTERACTION_RADIUS', radius)
    _patch_float(
        sensor.op('interaction_field_PIXEL'),
        'interactionFalloff', 'FLEXGPU_INTERACTION_FALLOFF', falloff)
    _patch_float(
        sensor.op('interaction_field_PIXEL'),
        'forceGain', 'FLEXGPU_FORCE_GAIN', strength)
    _patch_float(
        sensor.op('INTERACTION_SMOOTH_PIXEL'),
        'interactionSmoothing',
        'FLEXGPU_INTERACTION_SMOOTHING', smoothing)
    _patch_float(
        sensor.op('INTERACTION_SMOOTH_PIXEL'),
        'interactionResponse',
        'FLEXGPU_INTERACTION_RESPONSE', response)
    _patch_float(
        sensor.op('INTERACTION_SMOOTH_PIXEL'),
        'interactionDecay',
        'FLEXGPU_INTERACTION_DECAY', decay)

def _apply_quality_profile():
    controls = _controls()
    profile = str(_value('Qualityprofile', '3080ti_16gb'))
    presets = {
        '3080ti_16gb': (384, 147456, 4.2, 5,
                        '147k adaptive: 384x384 or 512x288 / 5 Hz'),
        '4090': (512, 250000, 3.4, 10, 'Stream 512+ / geometry 512 / 10 Hz'),
        '5090': (512, 262144, 3.0, 15, 'Stream 512+ / geometry 512 / 15 Hz'),
    }
    geometry, points, point_size, geometry_fps, hint = presets.get(
        profile, presets['3080ti_16gb'])
    _set(controls, 'Geometryresolution', geometry)
    _set(controls, 'Pointbudget', points)
    _set(controls, 'Pointsize', point_size)
    _set(controls, 'Geometryfps', geometry_fps)
    _set(controls, 'Profilehint', hint)
    for name in ('Geometryresolution', 'Pointbudget', 'Pointsize', 'Geometryfps'):
        apply_parameter(name)

def _activate_geometry_bridge(provider):
    pipeline = _pipeline()
    adapter = pipeline.op('SOURCES/STREAMDIFFUSION_ADAPTER')
    selected = str(provider).strip().lower()
    bridge_name = (
        'DEPTH_ANYTHING_GEOMETRY_BRIDGE'
        if selected == 'depth_anything' else 'MOGE2_BRIDGE')
    bridge = adapter.op(bridge_name) if adapter is not None else None
    if bridge is None:
        return False
    _set(bridge, 'Enabled', True)
    runtime_dat = bridge.op('bridge_runtime')
    if runtime_dat is not None:
        try:
            runtime_dat.module.tick(bridge)
        except Exception:
            # A saved TOE can still be compiling its embedded module. The
            # bridge's frame callback retries after compilation completes, and
            # the external worker now waits for the listener instead of
            # failing immediately.
            pass
    return True

def _apply_vr_controls():
    controls = _controls()
    pipeline = _pipeline()
    vr = pipeline.op('VR_OUTPUT')
    render = pipeline.op('POINT_RENDER')
    experience = str(
        _value('Experience', 'installation')).strip().lower()
    if experience not in ('installation', 'vr', 'combined'):
        experience = 'installation'
    source = str(_value('Vrinputsource', 'mock')).strip().lower()
    if source not in ('mock', 'openvr'):
        source = 'mock'
    enabled = experience in ('vr', 'combined')
    target_hz = max(60, min(144, int(_value('Vrtargethz', 72))))
    eye_width = max(320, min(4096, int(_value('Vreyewidth', 1280))))
    eye_height = max(180, min(4096, int(_value('Vreyeheight', 720))))
    ipd = max(0.05, min(0.08, float(_value('Vripdmetres', 0.064))))
    fov = max(30.0, min(130.0, float(_value('Vrfovdegrees', 75.0))))
    head_values = {}
    for name, fallback, lower, upper in (
            ('Vrheadxmetres', 0.0, -5.0, 5.0),
            ('Vrheadymetres', 0.0, -5.0, 5.0),
            ('Vrheadzmetres', 0.0, -5.0, 5.0),
            ('Vrheadyawdegrees', 0.0, -180.0, 180.0),
            ('Vrheadpitchdegrees', 0.0, -89.0, 89.0),
            ('Vrheadrolldegrees', 0.0, -180.0, 180.0)):
        head_values[name] = max(
            lower, min(upper, float(_value(name, fallback))))
    hands_enabled = bool(_value('Vrhandenabled', False))
    hand_gain = max(
        0.0, min(2.0, float(_value('Vrhandgain', 0.65))))
    hand_values = {}
    for side, sign in (('left', -1.0), ('right', 1.0)):
        for axis, fallback, lower, upper in (
                ('x', 0.28 * sign, -3.0, 3.0),
                ('y', 0.02, -3.0, 3.0),
                ('z', -1.15, -5.0, 0.0)):
            name = 'Vr%shand%smetres' % (side, axis)
            hand_values[name] = max(
                lower, min(upper, float(_value(name, fallback))))

    for name, value in (
            ('Experience', experience), ('Vrinputsource', source),
            ('Vrtargethz', target_hz), ('Vreyewidth', eye_width),
            ('Vreyeheight', eye_height), ('Vripdmetres', ipd),
            ('Vrfovdegrees', fov), ('Vrhandenabled', hands_enabled),
            ('Vrhandgain', hand_gain)):
        _set(controls, name, value)
    for name, value in list(head_values.items()) + list(hand_values.items()):
        _set(controls, name, value)

    _set(vr, 'Enabled', enabled)
    _set(vr, 'Inputsource', source)
    _set(vr, 'Targethz', target_hz)
    _set(vr, 'Eyewidth', eye_width)
    _set(vr, 'Eyeheight', eye_height)
    _set(vr, 'Ipdmetres', ipd)
    _set(vr, 'Fovdegrees', fov)
    _set(vr, 'Handsenabled', hands_enabled)
    _set(vr, 'Handgain', hand_gain)
    for source_name, target_name in (
            ('Vrheadxmetres', 'Headxmetres'),
            ('Vrheadymetres', 'Headymetres'),
            ('Vrheadzmetres', 'Headzmetres'),
            ('Vrheadyawdegrees', 'Headyawdegrees'),
            ('Vrheadpitchdegrees', 'Headpitchdegrees'),
            ('Vrheadrolldegrees', 'Headrolldegrees')):
        _set(vr, target_name, head_values[source_name])
        _set(render, source_name, head_values[source_name])
    for side in ('left', 'right'):
        title = side.title()
        for axis in ('x', 'y', 'z'):
            source_name = 'Vr%shand%smetres' % (side, axis)
            _set(vr, title + 'hand' + axis + 'metres',
                 hand_values[source_name])
    _set(render, 'Vrenabled', enabled)
    _set(render, 'Vrinputsource', source)
    _set(render, 'Ipdmetres', ipd)
    _set(render, 'Vrfovdegrees', fov)

    for path in (
            'POINT_RENDER/METRIC_RENDER_LEFT_EYE',
            'POINT_RENDER/METRIC_RENDER_RIGHT_EYE'):
        _set_resolution(pipeline.op(path), eye_width, eye_height)
    hands_shader = pipeline.op('VR_OUTPUT/MOCK_HAND_POSITIONS_PIXEL')
    _patch_float(
        hands_shader, 'mockHandsEnabled', 'FLEXGPU_VR_HANDS_ENABLED',
        1.0 if (enabled and source == 'mock' and hands_enabled) else 0.0)
    _patch_vec4(
        hands_shader, 'mockLeftHand', 'FLEXGPU_VR_LEFT_HAND',
        (hand_values['Vrlefthandxmetres'],
         hand_values['Vrlefthandymetres'],
         hand_values['Vrlefthandzmetres'], 1.0))
    _patch_vec4(
        hands_shader, 'mockRightHand', 'FLEXGPU_VR_RIGHT_HAND',
        (hand_values['Vrrighthandxmetres'],
         hand_values['Vrrighthandymetres'],
         hand_values['Vrrighthandzmetres'], 1.0))
    _patch_float(
        pipeline.op('SENSOR_INTERACTION/interaction_field_PIXEL'),
        'vrHandGain', 'FLEXGPU_VR_HAND_GAIN', hand_gain)

    if not enabled:
        status = 'installation only; mock VR disabled'
    elif source == 'mock':
        status = (
            'desktop simulation at %d Hz target; no headset compositor' %
            target_hz)
    else:
        status = (
            'Quest/OpenVR requested but no headset adapter is installed; '
            'outputs remain fail-closed desktop previews')
    _set(vr, 'Status', status)
    _set(controls, 'Vrstatus', status)
    try:
        pipeline.store('vr_experience_mode', experience)
        pipeline.store('vr_provider', source)
        pipeline.store('vr_headset_validated', False)
    except Exception:
        pass

def _reset_vr_head_pose():
    for name in (
            'Vrheadxmetres', 'Vrheadymetres', 'Vrheadzmetres',
            'Vrheadyawdegrees', 'Vrheadpitchdegrees',
            'Vrheadrolldegrees'):
        _set(_controls(), name, 0.0)
    _apply_vr_controls()

def _reset_vr_hands():
    for name, value in (
            ('Vrlefthandxmetres', -0.28),
            ('Vrlefthandymetres', 0.02),
            ('Vrlefthandzmetres', -1.15),
            ('Vrrighthandxmetres', 0.28),
            ('Vrrighthandymetres', 0.02),
            ('Vrrighthandzmetres', -1.15)):
        _set(_controls(), name, value)
    _apply_vr_controls()

def _switch_runtime_geometry_contract(provider):
    root = _pipeline().parent()
    helpers = root.op('STARTUP/runtime_helpers') if root is not None else None
    if helpers is None:
        return False
    try:
        return bool(helpers.module.select_geometry_provider(root, provider))
    except Exception:
        # During a cold compile, startup/frame callbacks will finish loading
        # runtime_helpers. Visual routing remains fail-closed until its strict
        # metadata contract can be selected.
        return False

def apply_parameter(name):
    pipeline = _pipeline()
    key = str(name).lower()
    if key in (
            'experience', 'vrinputsource', 'vrtargethz',
            'vreyewidth', 'vreyeheight', 'vripdmetres',
            'vrfovdegrees', 'vrheadxmetres', 'vrheadymetres',
            'vrheadzmetres', 'vrheadyawdegrees',
            'vrheadpitchdegrees', 'vrheadrolldegrees',
            'vrhandenabled', 'vrhandgain',
            'vrlefthandxmetres', 'vrlefthandymetres',
            'vrlefthandzmetres', 'vrrighthandxmetres',
            'vrrighthandymetres', 'vrrighthandzmetres'):
        _apply_vr_controls()
    elif key == 'geometryprovider':
        provider = _value('Geometryprovider', 'moge2')
        _activate_geometry_bridge(provider)
        _set(pipeline.op('SOURCES/STREAMDIFFUSION_ADAPTER'),
             'Geometrysource', provider)
        _switch_runtime_geometry_contract(provider)
        _apply_point_cloud_scale()
    elif key in ('audioenabled', 'audiosource'):
        _apply_audio_controls()
    elif key in (
            'camerainteractionenabled', 'camerasensorsource',
            'femtodeviceserial', 'cameramirrorhorizontal',
            'femtomirrorhorizontal'):
        _apply_camera_interaction()
        _apply_sensor_calibration_trim()
        _apply_femto_depth_gate()
    elif key in (
            'sensorpositionscale',
            'sensortrimxmetres', 'sensortrimymetres', 'sensortrimzmetres',
            'sensortrimyawdegrees', 'sensortrimpitchdegrees',
            'sensortrimrolldegrees',
            'femtopositionscale',
            'femtotrimxmetres', 'femtotrimymetres', 'femtotrimzmetres',
            'femtotrimyawdegrees', 'femtotrimpitchdegrees',
            'femtotrimrolldegrees'):
        _apply_sensor_calibration_trim()
    elif key in (
            'femtoaudiencenearmetres', 'femtoaudiencefarmetres'):
        _apply_femto_depth_gate()
    elif key == 'displaymode':
        _set(pipeline, 'Displaymode', _value('Displaymode', 'single'))
    elif key == 'completionmode':
        _set(pipeline.op('COMPLETION'), 'Mode',
             _value('Completionmode', 'hybrid'))
    elif key == 'fogdensity':
        _apply_fog()
    elif key in (
            'brightness', 'contrast', 'saturation', 'gamma',
            'hueshiftdegrees', 'temperature', 'tint'):
        _apply_color_grade()
    elif key in (
            'interactionradius', 'interactionfalloff',
            'interactionstrength', 'interactionsmoothing',
            'interactionresponse', 'interactiondecay'):
        _apply_interaction_shape()
    elif key in (
            'installationinteractionenabled',
            'installationinteractionintensity',
            'leftwallinteractionenabled',
            'leftwallinteractionintensity',
            'centerwallinteractionenabled',
            'centerwallinteractionintensity',
            'rightwallinteractionenabled',
            'rightwallinteractionintensity'):
        _apply_output_interaction()
    elif key in (
            'wrapyawdegrees', 'wrapfovdegrees',
            'surfacefovdegrees', 'artisticyawdegrees',
            'artisticoffsetmetres'):
        parameter, fallback, lower, upper = {
            'wrapyawdegrees': ('Wrapyawdegrees', 30.0, -89.0, 89.0),
            'wrapfovdegrees': ('Wrapfovdegrees', 78.0, 10.0, 170.0),
            'surfacefovdegrees': ('Surfacefovdegrees', 60.0, 10.0, 170.0),
            'artisticyawdegrees': ('Artisticyawdegrees', 18.0, -89.0, 89.0),
            'artisticoffsetmetres': (
                'Artisticoffsetmetres', 0.45, -5.0, 5.0),
        }[key]
        value = max(lower, min(upper, float(_value(parameter, fallback))))
        _set(pipeline.op('POINT_RENDER'), parameter, value)
        _set(_controls(), parameter, value)
    elif key == 'artisticoffsetdirection':
        _apply_artistic_offset_direction()
    elif key in (
            'leftwallscale', 'centerwallscale', 'rightwallscale',
            'leftwallpanhorizontaldegrees', 'leftwallpanverticaldegrees',
            'centerwallpanhorizontaldegrees', 'centerwallpanverticaldegrees',
            'rightwallpanhorizontaldegrees', 'rightwallpanverticaldegrees'):
        _apply_wall_view_control(key)
    elif key in ('wallwidth', 'wallheight'):
        _apply_wall_resolution()
    elif key in ('pointcloudscale', 'moge2scale', 'depthanythingscale'):
        _apply_point_cloud_scale()
    elif key in ('wrapcoverage', 'wrapnoise'):
        parameter = 'Wrapcoverage' if key == 'wrapcoverage' else 'Wrapnoise'
        symbol = 'wrapCoverage' if key == 'wrapcoverage' else 'wrapNoise'
        marker = 'FLEXGPU_WRAP_COVERAGE' if key == 'wrapcoverage' else \
                 'FLEXGPU_WRAP_NOISE'
        value = max(0.0, min(1.0, float(_value(parameter, 0.4))))
        _set(pipeline.op('TRIPLE_DISPLAY'), parameter, value)
        for side in ('LEFT', 'CENTER', 'RIGHT'):
            _patch_float(
                pipeline.op('TRIPLE_DISPLAY/COVERAGE_WRAP_%s_PIXEL' % side),
                symbol, marker, value)
    elif key == 'qualityprofile':
        _apply_quality_profile()
    elif key == 'geometryresolution':
        _apply_geometry_contract_resolution()
    elif key == 'preservegeometryaspect':
        _apply_geometry_contract_resolution()
    elif key == 'pointbudget':
        points = max(1000, min(10000000, int(_value('Pointbudget', 120000))))
        _set(pipeline.op('POINT_RENDER'), 'Maxpoints', points)
        _set(_pipeline().parent().op('WORLD_CORE'), 'Pointbudget', points)
    elif key == 'pointsize':
        _set(pipeline.op('POINT_RENDER'), 'Pointsize',
             max(0.25, min(128.0, float(_value('Pointsize', 4.2)))))
    elif key == 'geometryfps':
        fps = max(1, min(60, int(_value('Geometryfps', 5))))
        for bridge_name in ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE'):
            bridge = pipeline.op(
                'SOURCES/STREAMDIFFUSION_ADAPTER/' + bridge_name)
            _set(bridge, 'Capturefps', fps)
            _set(bridge, 'Profile', _value('Qualityprofile', '3080ti_16gb'))

def apply_all():
    for name in (
        'Experience', 'Vrinputsource', 'Vrtargethz',
        'Vreyewidth', 'Vreyeheight', 'Vripdmetres',
        'Vrfovdegrees', 'Vrheadxmetres', 'Vrheadymetres',
        'Vrheadzmetres', 'Vrheadyawdegrees',
        'Vrheadpitchdegrees', 'Vrheadrolldegrees',
        'Vrhandenabled', 'Vrhandgain',
        'Vrlefthandxmetres', 'Vrlefthandymetres',
        'Vrlefthandzmetres', 'Vrrighthandxmetres',
        'Vrrighthandymetres', 'Vrrighthandzmetres',
        'Geometryprovider', 'Audioenabled', 'Audiosource',
        'Camerainteractionenabled', 'Camerasensorsource',
        'Femtodeviceserial', 'Cameramirrorhorizontal',
        'Femtomirrorhorizontal',
        'Sensorpositionscale',
        'Sensortrimxmetres', 'Sensortrimymetres', 'Sensortrimzmetres',
        'Sensortrimyawdegrees', 'Sensortrimpitchdegrees',
        'Sensortrimrolldegrees',
        'Femtopositionscale',
        'Femtotrimxmetres', 'Femtotrimymetres', 'Femtotrimzmetres',
        'Femtotrimyawdegrees', 'Femtotrimpitchdegrees',
        'Femtotrimrolldegrees',
        'Femtoaudiencenearmetres', 'Femtoaudiencefarmetres',
        'Displaymode', 'Completionmode', 'Fogdensity',
        'Brightness', 'Contrast', 'Saturation', 'Gamma',
        'Hueshiftdegrees', 'Temperature', 'Tint',
        'Interactionradius', 'Interactionfalloff',
        'Interactionstrength', 'Interactionsmoothing',
        'Interactionresponse', 'Interactiondecay',
        'Installationinteractionenabled',
        'Installationinteractionintensity',
        'Leftwallinteractionenabled', 'Leftwallinteractionintensity',
        'Centerwallinteractionenabled', 'Centerwallinteractionintensity',
        'Rightwallinteractionenabled', 'Rightwallinteractionintensity',
        'Wrapyawdegrees',
        'Wrapfovdegrees', 'Wrapcoverage', 'Wrapnoise',
        'Surfacefovdegrees', 'Artisticyawdegrees',
        'Artisticoffsetdirection', 'Artisticoffsetmetres',
        'Wallwidth', 'Wallheight', 'Pointcloudscale',
        'Leftwallscale', 'Centerwallscale', 'Rightwallscale',
        'Leftwallpanhorizontaldegrees', 'Leftwallpanverticaldegrees',
        'Centerwallpanhorizontaldegrees', 'Centerwallpanverticaldegrees',
        'Rightwallpanhorizontaldegrees', 'Rightwallpanverticaldegrees',
        'Moge2scale', 'Depthanythingscale',
        'Geometryresolution', 'Preservegeometryaspect',
        'Pointbudget', 'Pointsize', 'Geometryfps'):
        apply_parameter(name)

def onValueChange(par, prev):
    apply_parameter(par.name)
    return

def onPulse(par):
    key = str(par.name).lower()
    if key == 'applyall':
        apply_all()
    elif key == 'resetvrheadpose':
        _reset_vr_head_pose()
    elif key == 'resetvrhands':
        _reset_vr_hands()
    elif key == 'resetcolor':
        _reset_color_grade()
    elif key == 'resetsensorcalibrationtrim':
        _reset_sensor_calibration_trim('Sensor')
    elif key == 'resetfemtocalibrationtrim':
        _reset_sensor_calibration_trim('Femto')
    elif key == 'startmogeworker':
        _launch_worker('moge2')
    elif key == 'stopmogeworker':
        _stop_worker('moge2')
    elif key == 'startdepthanythingworker':
        _launch_worker('depth_anything')
    elif key == 'stopdepthanythingworker':
        _stop_worker('depth_anything')
    elif key == 'startcameradepthworker':
        _launch_sensor_worker()
    elif key == 'stopcameradepthworker':
        _stop_sensor_worker()
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


def _value(node, names, fallback=None):
    if isinstance(names, str):
        names = (names,)
    parameter = _par(node, *names)
    if parameter is None:
        return fallback
    try:
        return parameter.eval()
    except Exception:
        try:
            return parameter.val
        except Exception:
            return fallback


def _patch_shader_float(dat, symbol, marker, value):
    """Patch one marked GLSL float constant without changing other source."""

    if dat is None:
        return False
    number = r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
    pattern = (
        r"(const\s+float\s+" + re.escape(str(symbol)) + r"\s*=\s*)" +
        number + r"(\s*;\s*//\s*" + re.escape(str(marker)) + r")")
    try:
        updated, count = re.subn(
            pattern, r"\g<1>" + ("%.6g" % float(value)) + r"\g<2>",
            str(dat.text), count=1)
        if count == 1:
            dat.text = updated
            return True
    except Exception:
        pass
    return False


def _patch_shader_vec4(dat, symbol, marker, values):
    """Patch one marked GLSL vec4 constant without changing other source."""

    if dat is None:
        return False
    try:
        numbers = [float(value) for value in values]
    except Exception:
        return False
    if len(numbers) != 4 or not all(math.isfinite(value) for value in numbers):
        return False
    pattern = (
        r"(const\s+vec4\s+" + re.escape(str(symbol)) +
        r"\s*=\s*vec4\()[^)]*(\)\s*;\s*//\s*" +
        re.escape(str(marker)) + r")")
    replacement = (
        r"\g<1>" +
        ", ".join("%.9g" % value for value in numbers) +
        r"\g<2>")
    try:
        updated, count = re.subn(
            pattern, replacement, str(dat.text), count=1)
        if count == 1:
            dat.text = updated
            return True
    except Exception:
        pass
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
        if replace:
            dst.inputConnectors[dst_index].disconnect()
        src.outputConnectors[src_index].connect(
            dst.inputConnectors[dst_index])
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


def _set_parameter_bounds(
        parameter, minimum=None, maximum=None, clamp=True):
    """Set useful slider bounds while tolerating TouchDesigner API variants."""

    if parameter is None:
        return
    for attribute, value in (
            ("min", minimum), ("normMin", minimum),
            ("max", maximum), ("normMax", maximum)):
        if value is None:
            continue
        try:
            setattr(parameter, attribute, value)
        except Exception:
            pass
    if minimum is not None:
        try:
            parameter.clampMin = bool(clamp)
        except Exception:
            pass
    if maximum is not None:
        try:
            parameter.clampMax = bool(clamp)
        except Exception:
            pass


def _custom(
        comp, page, kind, name, default, menu=None, label=None,
        minimum=None, maximum=None, clamp=True):
    existing = _par(comp, name)
    if existing is not None:
        if menu and kind == "Menu":
            try:
                existing.menuNames = list(menu)
                existing.menuLabels = [
                    str(value).replace("_", " ").title() for value in menu]
            except Exception:
                pass
        _set_parameter_bounds(
            existing, minimum=minimum, maximum=maximum, clamp=clamp)
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
        _set_parameter_bounds(
            parameter, minimum=minimum, maximum=maximum, clamp=clamp)
        return parameter
    except Exception:
        return None


def _ensure_audio_adapter_contract(adapter):
    """Expose optional audio controls without owning tracks or private nodes."""

    if adapter is None:
        return None, None
    page = _page(adapter, "Optional Audio")
    enabled = _custom(
        adapter, page, "Toggle", "Audioenabled",
        bool(_value(adapter, "Audioenabled", False)),
        label="Audio Enabled")
    source = _custom(
        adapter, page, "Menu", "Audiosource",
        str(_value(adapter, "Audiosource", "voices")),
        ("voices", "soundscape"), label="Audio Source")
    if source is not None:
        try:
            source.menuLabels = [
                "Human Voices Only",
                "Soundscape Only",
            ]
        except Exception:
            pass
    return enabled, source


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
    # A horizontal three-wall mosaic must use its 16:3 pixel resolution as
    # the display aspect.  Inheriting a 16:9 wall input gives the 5760x1080
    # texture non-square pixels and makes viewers/mappers treat it as
    # 5760x3240.  Resolution aspect is also the deterministic choice for the
    # other explicit geometry and output textures created by this helper.
    _set(node, "outputaspect", "resolution")


def _geometry_contract_dimensions(pipeline):
    """Return the numeric position-texture contract for mixed-resolution GLSL."""

    reconstruction = (
        pipeline.op("RECONSTRUCTION") if pipeline is not None else None)
    geometry = max(
        64, min(
            2048,
            int(_value(reconstruction, "Geometryresolution", 384))))
    preserve = bool(
        _value(reconstruction, "Preservegeometryaspect", True))
    aspect = 16.0 / 9.0
    rgb = (
        reconstruction.op("RGB_IN")
        if reconstruction is not None else None)
    try:
        if rgb is not None and rgb.width > 0 and rgb.height > 0:
            aspect = max(1.0 / 16.0, min(
                16.0, float(rgb.width) / float(rgb.height)))
    except Exception:
        pass
    if not preserve:
        return geometry, geometry
    width = max(
        64, min(
            2048,
            2 * int(round((geometry * aspect ** 0.5) / 2.0))))
    height = max(
        64, min(
            2048,
            2 * int(round((geometry / aspect ** 0.5) / 2.0))))
    return width, height


def _align_interaction_position_resolutions(pipeline):
    """Keep low-res interaction inputs from reducing the position contract."""

    width, height = _geometry_contract_dimensions(pipeline)
    for path in (
            "COMPLETION/INTERACTION_RENDER_RESIZE",
            "COMPLETION/procedural_backfill",
            "POINT_RENDER/INTERACTION_RENDER_RESIZE",
            "POINT_RENDER/VIEW_POSITION_INSTALLATION",
            "POINT_RENDER/VIEW_POSITION_LEFT",
            "POINT_RENDER/VIEW_POSITION_CENTER",
            "POINT_RENDER/VIEW_POSITION_RIGHT"):
        node = pipeline.op(path)
        if node is not None:
            _set_resolution(node, width, height)
    return width, height


def _set_horizontal_layout(node):
    """Arrange Layout TOP inputs left-to-right with a fallback for old hosts."""

    if node is None:
        return
    if getattr(getattr(node, "par", None), "align", None) is not None:
        # Layout TOP uses the menu name ``horizlr`` (label: Left to Right).
        # ``horizontal`` is not a valid value and silently leaves Align=None.
        _set(node, "align", "horizlr")
    else:
        _set(node, "direction", "horizontal")


def _scaled_camera_fov_expression(
        base_expression, wall_scale_expression="1.0"):
    """Return a perspective-correct view-scale expression for a Camera COMP."""

    return (
        "2.0 * math.degrees(math.atan("
        "math.tan(math.radians(%s) * 0.5) / "
        "max(0.2, min(10.0, "
        "parent().par.Pointcloudscale.eval() * (%s)))))"
        % (base_expression, wall_scale_expression)
    )


def _apply_point_cloud_camera_framing(render):
    """Bind every managed camera to one provider-aware point-cloud scale."""

    if render is None:
        return
    render_page = _page(render, "Render")
    _custom(
        render, render_page, "Float", "Pointcloudscale", 1.0,
        label="Effective Point Cloud View Scale")
    for side in ("Left", "Center", "Right"):
        _custom(
            render, render_page, "Float", side + "wallscale", 1.0,
            label=side + " Wall View Scale")
        _custom(
            render, render_page, "Float",
            side + "wallpanhorizontaldegrees", 0.0,
            label=side + " Camera Horizontal Pan (degrees)")
        _custom(
            render, render_page, "Float",
            side + "wallpanverticaldegrees", 0.0,
            label=side + " Camera Vertical Pan (degrees)")
    for camera_name in (
        "CAMERA_CENTER_METRIC", "CAMERA_LEFT_METRIC", "CAMERA_RIGHT_METRIC",
    ):
        _expr(
            render.op(camera_name), "fov",
            _scaled_camera_fov_expression("60.0"))
    wrap_yaw = {
        "LEFT": "parent().par.Wrapyawdegrees.eval()",
        "CENTER": "0.0",
        "RIGHT": "-parent().par.Wrapyawdegrees.eval()",
    }
    artistic_yaw = {
        "LEFT": "-parent().par.Artisticyawdegrees.eval()",
        "CENTER": "0.0",
        "RIGHT": "parent().par.Artisticyawdegrees.eval()",
    }
    for side in ("LEFT", "CENTER", "RIGHT"):
        wall_scale_expression = (
            "parent().par.%swallscale.eval()" % side.title())
        pan_horizontal = (
            "parent().par.%swallpanhorizontaldegrees.eval()" % side.title())
        pan_vertical = (
            "parent().par.%swallpanverticaldegrees.eval()" % side.title())
        for mode, base_yaw, base_fov in (
                ("WRAP", wrap_yaw[side],
                 "parent().par.Wrapfovdegrees.eval()"),
                ("ARTISTIC", artistic_yaw[side],
                 "parent().par.Surfacefovdegrees.eval()")):
            camera = render.op("CAMERA_" + mode + "_" + side)
            _expr(camera, "rx", pan_vertical)
            _expr(
                camera, "ry",
                "(%s) + (%s)" % (base_yaw, pan_horizontal))
            _expr(
                camera, "fov",
                _scaled_camera_fov_expression(
                    base_fov, wall_scale_expression))


def _embedded_runtime_source(filename, label, report):
    """Load a runtime and bind this checkout's public ``src`` as a cold hint."""

    touchdesigner_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(touchdesigner_dir, filename)
    try:
        size = os.path.getsize(path)
        if size < 1 or size > 512 * 1024:
            raise ValueError("%s runtime source is outside the size limit" % label)
        with open(path, "r", encoding="utf-8") as stream:
            source = stream.read()
        marker = '_EMBEDDED_FLEXGPU_SRC = ""'
        if source.count(marker) != 1:
            raise ValueError("%s runtime source hint marker is invalid" % label)
        src_hint = os.path.abspath(os.path.join(touchdesigner_dir, "..", "src"))
        if not os.path.isfile(os.path.join(src_hint, "flexgpu", "worldbus.py")):
            raise ValueError("%s runtime source tree is incomplete" % label)
        return source.replace(
            marker,
            "_EMBEDDED_FLEXGPU_SRC = %r" % src_hint,
            1,
        )
    except Exception as exc:
        report.warn("%s runtime could not be embedded: %s" % (label, exc))
        return ("def tick(bridge_comp=None):\n    return None\n\n"
                "def stop(bridge_comp=None):\n    return None\n\n"
                "def on_script_top_cook(script_op):\n    return None\n")


def _moge2_runtime_source(report):
    """Load the import-safe bridge module that will be embedded in a Text DAT."""

    return _embedded_runtime_source(
        "moge2_bridge_runtime.py",
        "MoGe-2 bridge",
        report,
    )


def _build_moge2_bridge(
        adapter, input_rgb, report, name="MOGE2_BRIDGE", provider="moge2",
        input_tcp=9211, input_udp=9210, result_tcp=9221, result_udp=9220):
    """Build a default-off generated-image geometry bridge.

    The MoGe and Depth Anything providers intentionally share the same
    synchronized atlas decoder but use distinct provider identities and ports.
    No model runtime is imported into TouchDesigner.
    """

    is_depth_anything = provider == "depth_anything"
    title = "Depth Anything Geometry" if is_depth_anything else "MoGe-2"
    comp = _ensure(adapter, "baseCOMP", name, report)
    _style(
        comp, -350 if is_depth_anything else 360,
        -50 if is_depth_anything else 180,
        (0.20, 0.48, 0.42) if is_depth_anything else (0.20, 0.39, 0.56),
        "Opt-in %s worker: newest RGB -> synchronized RGB/pseudo-metric depth"
        % title,
        330, 135)
    page = _page(comp, title + " Bridge")
    _custom(comp, page, "Toggle", "Enabled", False)
    provider_par = _custom(
        comp, page, "Str", "Provider", provider, label="Geometry Provider")
    _set(comp, provider_par.name if provider_par is not None else "Provider", provider)
    _custom(comp, page, "Menu", "Profile", "3080ti_16gb",
            ("3080ti_16gb", "4090", "5090"))
    _custom(comp, page, "Str", "Workerhost", "127.0.0.1",
            label="Worker Host")
    _custom(comp, page, "Int", "Workerinputtcp", input_tcp,
            label="Worker Input TCP")
    _custom(comp, page, "Int", "Workerinputudp", input_udp,
            label="Worker Input UDP")
    _custom(comp, page, "Str", "Resultbindhost", "127.0.0.1",
            label="Result Bind Host")
    _custom(comp, page, "Int", "Resulttcp", result_tcp,
            label="Result TCP")
    _custom(comp, page, "Int", "Resultudp", result_udp,
            label="Result UDP")
    _set(comp, "Workerinputtcp", input_tcp)
    _set(comp, "Workerinputudp", input_udp)
    _set(comp, "Resulttcp", result_tcp)
    _set(comp, "Resultudp", result_udp)
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
          ("%s LIVE GEOMETRY BRIDGE (DEFAULT OFF)\n\n" % title.upper()) +
          "IN_RGB must be the exact StreamDiffusionTD image. The external worker "
          "owns the model and returns a WorldBus rgba8_atlas. This COMP publishes "
          "the returned RGB with its depth/mask/confidence so frames cannot "
          "cross. The MoGe provider's returned RGB with its metric depth remains "
          "synchronized. Frames cannot "
          "cross. TD operator access remains on the main thread; socket threads move "
          "bounded immutable bytes only. FRAME_STATE and CAMERA_METADATA describe "
          "the same confirmed atlas upload. Resultvalid stays false until a forced "
          "Script TOP cook copies the exact staged key. Do not put secrets or prompt "
          "text in metadata.",
          report)
    try:
        comp.store("generated_geometry_bridge_runtime_dat", runtime_dat.path)
    except Exception:
        pass
    # The bounded installer creates operators through regular TD Python, which
    # does not auto-place them. Keep the generated bridge readable without
    # moving any private or user-authored operator.
    _style(rgb_in, -500, 250, (0.20, 0.48, 0.42), "Generated RGB input")
    _style(atlas, -275, 250, (0.20, 0.48, 0.42), "Synchronized atlas")
    _style(scale_bias, -275, 50, (0.20, 0.48, 0.42), "Depth scale / bias")
    for index, node in enumerate((rgb, depth, confidence, mask)):
        y = 350 - index * 200
        _style(node, 0, y, (0.20, 0.48, 0.42), node.name)
        output = comp.op(("OUT_RGB", "OUT_DEPTH", "OUT_CONFIDENCE", "OUT_MASK")[index])
        _style(output, 250, y, (0.20, 0.48, 0.42), output.name)
        shader_source = comp.op((
            "UNPACK_RGB_PIXEL", "UNPACK_DEPTH_METRES_PIXEL",
            "UNPACK_CONFIDENCE_PIXEL", "UNPACK_MASK_PIXEL")[index])
        _style(shader_source, 0, y + 110, (0.16, 0.32, 0.28), "Managed shader")
    for index, node in enumerate((
            runtime_dat, execute, script_callbacks,
            comp.op("FRAME_STATE"), comp.op("CAMERA_METADATA"),
            comp.op("STATUS"), comp.op("README_FIRST"))):
        if node is not None:
            _style(node, -500 if index < 3 else 500,
                   50 - (index if index < 3 else index - 3) * 150,
                   (0.16, 0.32, 0.28), node.name)
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


def _build_depth_anything_geometry_bridge(adapter, input_rgb, report):
    """Build the isolated generated-image Depth Anything geometry provider."""

    return _build_moge2_bridge(
        adapter, input_rgb, report,
        name="DEPTH_ANYTHING_GEOMETRY_BRIDGE",
        provider="depth_anything",
        input_tcp=9251,
        input_udp=9250,
        result_tcp=9261,
        result_udp=9260,
    )


def _wire_generated_geometry_routes(adapter, depth_anything, moge2_routes, report):
    """Select MoGe or Depth Anything as one atomic geometry source.

    MoGe keeps its established fallback behavior. Selecting Depth Anything
    fails closed to zero until that provider confirms a fresh atlas, preventing
    accidental mixing with a stale MoGe or placeholder frame.
    """

    if len(moge2_routes) != 4 or any(node is None for node in moge2_routes):
        raise RuntimeError("generated geometry selection requires four MoGe routes")
    zero = _ensure(
        adapter, "constantTOP", "DEPTH_ANYTHING_GEOMETRY_FAIL_CLOSED_ZERO", report)
    _style(zero, 325, -500, (0.20, 0.48, 0.42),
           "Selected Depth Anything invalid -> zero")
    _set_resolution(zero, 384, 384)
    _set(zero, "format", "rgba32float")
    for names in (("colorr", "color1r"), ("colorg", "color1g"),
                  ("colorb", "color1b"), ("colora", "color1a", "alpha")):
        _set(zero, names, 0.0)
    route_specs = (
        ("GENERATED_GEOMETRY_RGB_ROUTE", moge2_routes[0], 0),
        ("GENERATED_GEOMETRY_DEPTH_ROUTE", moge2_routes[1], 1),
        ("GENERATED_GEOMETRY_CONFIDENCE_ROUTE", moge2_routes[2], 2),
        ("GENERATED_GEOMETRY_MASK_ROUTE", moge2_routes[3], 3),
    )
    routes = []
    selector = (
        "parent().par.Geometrysource.eval() == 'depth_anything'")
    valid = (
        "op('DEPTH_ANYTHING_GEOMETRY_BRIDGE').par.Enabled and "
        "op('DEPTH_ANYTHING_GEOMETRY_BRIDGE').par.Resultvalid")
    for name, moge2_route, output_index in route_specs:
        route = _ensure(adapter, "switchTOP", name, report)
        _connect(moge2_route, route, 0, 0, report, replace=True)
        _connect(depth_anything, route, 1, output_index, report, replace=True)
        _connect(zero, route, 2, 0, report, replace=True)
        _expr(route, "index",
              "((1 if (%s) else 2) if (%s) else 0)" % (valid, selector))
        _style(route, 325, (575, 275, -25, -350)[output_index],
               (0.20, 0.48, 0.42), name)
        routes.append(route)
    for index, (name, route) in enumerate(zip(
        ("OUT_RGB", "OUT_DEPTH", "OUT_CONFIDENCE", "OUT_MASK"), routes)):
        _out_top(adapter, name, route, index, report)
    return tuple(routes)


def _depth_anything_runtime_source(report):
    """Load the import-safe sensor receiver for embedding in a Text DAT."""

    return _embedded_runtime_source(
        "depth_anything_sensor_runtime.py",
        "Depth Anything sensor",
        report,
    )


def _build_femto_mega_adapter(sources, report):
    """Build a default-off native Orbbec/Femto sensor contract.

    The Orbbec TOP remains isolated from the existing webcam/Depth Anything
    bridge. No serial is embedded in tracked code; an empty serial auto-selects
    the first locally detected Orbbec device when this source is enabled.
    """

    comp = _ensure(sources, "baseCOMP", "FEMTO_MEGA_ADAPTER", report)
    _style(
        comp, 720, -260, (0.19, 0.44, 0.52),
        "Native Femto Mega pointcloud; separate default-off sensor source",
        310, 135)
    page = _page(comp, "Femto Mega Sensor")
    _custom(comp, page, "Toggle", "Enabled", False, label="Femto Mega Enabled")
    _custom(
        comp, page, "Str", "Deviceserial", "",
        label="Femto Mega Device Serial (optional)")
    result_valid = _custom(
        comp, page, "Toggle", "Resultvalid", False,
        label="Live Native Pointcloud")
    if result_valid is not None:
        try:
            result_valid.readOnly = True
        except Exception:
            pass

    primary = _ensure(
        comp, "orbbecTOP", "FEMTO_PRIMARY", report, optional=True)
    if primary is None:
        primary = _ensure(comp, "constantTOP", "FEMTO_UNAVAILABLE_ZERO", report)
        _set_resolution(primary, 640, 576)
        _set(primary, "format", "rgba32float")
        for names in (
                ("colorr", "color1r"), ("colorg", "color1g"),
                ("colorb", "color1b"), ("colora", "color1a", "alpha")):
            _set(primary, names, 0.0)
        _set(comp, "Resultvalid", False)
    else:
        _set(primary, "devicesource", "auto")
        _set(primary, "image", "pointcloud")
        _set(primary, "format", "rgba32float")
        try:
            current_device = str(primary.par.device.eval() or "")
            choices = [str(item) for item in primary.par.device.menuNames]
            if not current_device and choices:
                primary.par.device.val = choices[0]
        except Exception:
            pass
        _expr(primary, "active", "parent().par.Enabled")
        _expr(
            comp, "Resultvalid",
            "bool(parent().op('FEMTO_MEGA_ADAPTER').par.Enabled and "
            "parent().op('FEMTO_MEGA_ADAPTER/FEMTO_PRIMARY').valid and "
            "not parent().op('FEMTO_MEGA_ADAPTER/FEMTO_PRIMARY').errors() and "
            "parent().op('FEMTO_MEGA_ADAPTER/FEMTO_PRIMARY').width > 1 and "
            "parent().op('FEMTO_MEGA_ADAPTER/FEMTO_PRIMARY').height > 1)")

    position = _glsl(
        comp, "CONVERT_SENSOR_POSITION", "femto_sensor_position",
        [primary], report, True)
    validity = _glsl(
        comp, "DERIVE_SENSOR_VALIDITY", "femto_sensor_validity",
        [position], report)
    _set(validity, "format", "mono8fixed")
    _out_top(comp, "OUT_POSITION", position, 0, report)
    _out_top(comp, "OUT_MASK", validity, 1, report)
    _out_top(comp, "OUT_CONFIDENCE", validity, 2, report)
    _text(
        comp, "README_FIRST",
        "FEMTO MEGA SENSOR ADAPTER (DEFAULT OFF)\n\n"
        "This public adapter uses TouchDesigner's native Orbbec TOP in "
        "pointcloud mode. It converts native camera-local XYZ to the same "
        "sensor-local POSITION/MASK/CONFIDENCE contract used by the existing "
        "webcam + Depth Anything bridge. Raw pointcloud alpha is ignored; "
        "validity is derived from finite positive native depth. Device Serial "
        "is local and optional. The Show Control source selector enables only "
        "one sensor path at a time. Venue alignment remains in the shared "
        "Camera Calibration tab, so selecting Femto never changes or erases "
        "the saved webcam settings.",
        report)
    return comp


def _build_depth_anything_sensor_bridge(adapter, report):
    """Build a backend-replaceable, result-only audience sensor receiver."""

    comp = _ensure(adapter, "baseCOMP", "DEPTH_ANYTHING_BRIDGE", report)
    _style(comp, 390, 120, (0.20, 0.48, 0.42),
           "Replaceable no-RGB sensor bridge: packed depth/mask/confidence",
           320, 140)
    page = _page(comp, "Depth Anything Sensor")
    _custom(comp, page, "Toggle", "Enabled", False,
            label="Follow Adapter Enabled")
    # DEPTH_SENSOR_ADAPTER.Enabled remains the stable control surface used by
    # runtime_helpers, while Sensorsource prevents the webcam receiver from
    # cooking when the independent Femto Mega source is selected.
    _expr(
        comp, "Enabled",
        "parent().par.Enabled and "
        "parent().par.Sensorsource.eval() == 'depth_anything'")
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
    _custom(comp, page, "Toggle", "Mirrorhorizontal", True,
            label="Mirror Horizontal (Webcam)")
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
          "OUT_CONFIDENCE, and FRAME_STATE contracts. Sensor data is mirrored "
          "horizontally by default for intuitive laptop rehearsal; "
          "turn Mirror Horizontal off for an unmirrored calibrated show sensor. "
          "The packed depth, mask, confidence, principal point, and temporal "
          "session identity change together so the sensor contracts stay aligned. "
          "OUT_POSITION is strictly "
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


def _wire_depth_anything_sensor_routes(
        adapter, bridge, fallbacks, report, femto=None):
    """Route one selected sensor and fail closed while it is unavailable."""

    if len(fallbacks) != 3 or any(node is None for node in fallbacks):
        raise RuntimeError("Depth Anything routes require three adapter fallbacks")
    adapter_page = _page(adapter, "Adapter")
    _custom(
        adapter, adapter_page, "Menu", "Sensorsource", "depth_anything",
        ("depth_anything", "femto_mega"), label="Audience Sensor Source")
    zero = _ensure(adapter, "constantTOP", "DEPTH_ANYTHING_FAIL_CLOSED_ZERO", report)
    _set_resolution(zero, 256, 144)
    _set(zero, "format", "rgba32float")
    for names in (("colorr", "color1r"), ("colorg", "color1g"),
                  ("colorb", "color1b"), ("colora", "color1a", "alpha")):
        _set(zero, names, 0.0)
    if femto is None:
        try:
            femto = adapter.parent().parent().op("SOURCES/FEMTO_MEGA_ADAPTER")
        except Exception:
            femto = None
    femto_path = (
        femto.path if femto is not None
        else "/project1/flexgpu/WORKING_PIPELINE/SOURCES/FEMTO_MEGA_ADAPTER")
    femto_specs = (
        ("FEMTO_POSITION_SELECT", "OUT_POSITION"),
        ("FEMTO_MASK_SELECT", "OUT_MASK"),
        ("FEMTO_CONFIDENCE_SELECT", "OUT_CONFIDENCE"),
    )
    femto_selects = []
    for name, output_name in femto_specs:
        select = _ensure(adapter, "selectTOP", name, report)
        _set(select, ("top", "topop"), femto_path + "/" + output_name)
        femto_selects.append(select)
    route_specs = (
        ("DEPTH_ANYTHING_POSITION_ROUTE", fallbacks[0], 0, femto_selects[0]),
        ("DEPTH_ANYTHING_MASK_ROUTE", fallbacks[1], 1, femto_selects[1]),
        ("DEPTH_ANYTHING_CONFIDENCE_ROUTE", fallbacks[2], 2, femto_selects[2]),
    )
    routes = []
    for name, fallback, output_index, femto_select in route_specs:
        route = _ensure(adapter, "switchTOP", name, report)
        _connect(fallback, route, 0, 0, report, replace=True)
        _connect(zero, route, 1, 0, report, replace=True)
        _connect(bridge, route, 2, output_index, report, replace=True)
        _connect(femto_select, route, 3, 0, report, replace=True)
        _expr(route, "index",
              "0 if not parent().par.Enabled else "
              "(3 if (parent().par.Sensorsource.eval() == 'femto_mega' "
              "and op('%s').par.Resultvalid) else "
              "(2 if (parent().par.Sensorsource.eval() == 'depth_anything' "
              "and op('DEPTH_ANYTHING_BRIDGE').par.Resultvalid) else 1))" %
              femto_path)
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
    _custom(adapter, adapter_page, "Menu", "Geometrysource", "moge2",
            ("moge2", "depth_anything"), label="Generated Geometry Source")
    _custom(adapter, adapter_page, "Str", "RGBContract", TOP_CONTRACTS["RGB"])
    _custom(adapter, adapter_page, "Str", "DepthContract", TOP_CONTRACTS["DEPTH"])
    _ensure_audio_adapter_contract(adapter)
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
    moge2_routes = _wire_moge2_routes(
        adapter, moge2, (tox_rgb, tox_depth, tox_confidence, tox_mask), report)
    depth_anything_geometry = _build_depth_anything_geometry_bridge(
        adapter, tox_rgb, report)
    _wire_generated_geometry_routes(
        adapter, depth_anything_geometry, moge2_routes, report)
    _table(adapter, "ADAPTER_CONTRACT", [
        ["output", "required contract", "replace node"],
        ["OUT_RGB", TOP_CONTRACTS["RGB"], "GENERATED_GEOMETRY_RGB_ROUTE"],
        ["OUT_DEPTH", TOP_CONTRACTS["DEPTH"], "GENERATED_GEOMETRY_DEPTH_ROUTE"],
        ["OUT_CONFIDENCE", TOP_CONTRACTS["CONFIDENCE"],
         "GENERATED_GEOMETRY_CONFIDENCE_ROUTE"],
        ["OUT_MASK", "R valid mask normalized 0..1",
         "GENERATED_GEOMETRY_MASK_ROUTE"],
    ], report)
    _table(adapter, "OPTIONAL_AUDIO_CONTRACT", [
        ["control", "public contract"],
        ["Audioenabled", "mirrors optional show_control/Audioenabled"],
        ["Audiosource", "voices or soundscape; drives audiosource_switch"],
        ["audio_out", "one exclusive output downstream of audiosource_switch"],
    ], report)
    _text(adapter, "README_FIRST", "STREAMDIFFUSIONTD ADAPTER BOUNDARY\n\n"
          "Demo mode works without this branch. Later place StreamDiffusionTD.tox here, "
          "wire its image to OUT_RGB, depth estimate to OUT_DEPTH, and optional "
          "validity/confidence to OUT_CONFIDENCE. Increment Session Epoch when a "
          "model, prompt generation, calibration, or producer session changes. "
          "If the TOX emits only RGB, choose MoGe-2 or Depth Anything on "
          "Geometry Source; each bridge returns synchronized generated RGB and depth. "
          "Optional podcast audio remains local: Audio Enabled and Audio Source "
          "mirror a public show_control and an exclusive audiosource_switch when "
          "those operators exist, but no track path or audio asset is embedded. "
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
    _custom(comp, page, "Toggle", "Preservegeometryaspect", True,
            label="Preserve Source Aspect")
    _custom(comp, page, "Menu", "Depthmode", "normalized",
            ("normalized", "metric", "inverse"), label="Depth Convention")
    _custom(comp, page, "Float", "Depthscale", 1.0, label="Depth Scale")
    _custom(comp, page, "Float", "Depthbias", 0.0, label="Depth Bias")
    _custom(comp, page, "Float", "Nearmetres", 0.35, label="Near (metres)")
    _custom(comp, page, "Float", "Farmetres", 4.50, label="Far (metres)")
    _custom(comp, page, "Toggle", "Installationdepthoverride", False,
            label="Installation Depth Override")
    _custom(comp, page, "Float", "Installationdepthscale", 1.0,
            label="Installation Depth Scale")
    _custom(comp, page, "Float", "Installationdepthbias", 0.0,
            label="Installation Depth Bias")
    _custom(comp, page, "Float", "Installationnear", 0.35,
            label="Installation Near (metres)")
    _custom(comp, page, "Float", "Installationfar", 4.50,
            label="Installation Far (metres)")
    _custom(comp, page, "Toggle", "Depthanythingdepthoverride", True,
            label="Depth Anything Depth Override")
    _custom(comp, page, "Float", "Depthanythingdepthscale", 1.0,
            label="Depth Anything Depth Scale")
    _custom(comp, page, "Float", "Depthanythingdepthbias", 0.0,
            label="Depth Anything Depth Bias")
    _custom(comp, page, "Float", "Depthanythingnear", 0.5,
            label="Depth Anything Near (metres)")
    _custom(comp, page, "Float", "Depthanythingfar", 4.0,
            label="Depth Anything Far (metres)")
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
    aspect = "(max(1.0, op('RGB_IN').width) / max(1.0, op('RGB_IN').height))"
    width = (
        "parent().par.Geometryresolution if not "
        "parent().par.Preservegeometryaspect.eval() else "
        "max(64, min(2048, 2 * int(round((parent().par.Geometryresolution * "
        + aspect + " ** 0.5) / 2.0))))"
    )
    height = (
        "parent().par.Geometryresolution if not "
        "parent().par.Preservegeometryaspect.eval() else "
        "max(64, min(2048, 2 * int(round((parent().par.Geometryresolution / "
        + aspect + " ** 0.5) / 2.0))))"
    )
    _expr(color, ("resolutionw", "resw"), width)
    _expr(color, ("resolutionh", "resh"), height)
    confidence_aligned = _ensure(comp, "resolutionTOP", "CONFIDENCE_ALIGNED_RESIZE", report)
    _connect(confidence, confidence_aligned, report=report, replace=True)
    _set(confidence_aligned, "outputresolution", "custom")
    _set(confidence_aligned, "resmult", False)
    _expr(confidence_aligned, ("resolutionw", "resw"), width)
    _expr(confidence_aligned, ("resolutionh", "resh"), height)
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


def _build_interaction_smoothing(comp, interaction, report):
    """Add bounded GPU smoothing after the world-space interaction shader."""

    seed = _ensure(comp, "constantTOP", "INTERACTION_SMOOTH_SEED", report)
    _set_resolution(seed, 384, 384)
    _expr(seed, ("resolutionw", "resw"), "op('interaction_field').width")
    _expr(seed, ("resolutionh", "resh"), "op('interaction_field').height")
    _set(seed, "format", "rgba32float")
    for names in (("colorr", "color1r"), ("colorg", "color1g"),
                  ("colorb", "color1b"), ("colora", "color1a", "alpha")):
        _set(seed, names, 0.0)
    history = _ensure(comp, "feedbackTOP", "INTERACTION_SMOOTH_HISTORY", report)
    _connect(seed, history, 0, 0, report, replace=True)
    smoothed = _glsl(
        comp, "INTERACTION_SMOOTH", "interaction_smoothing",
        [interaction, history], report, True)
    _set(history, ("targettop", "target", "top"), smoothed.path)
    return smoothed


def _build_vr_output(parent, report):
    """Build the headset-independent VR adapter and deterministic mock hands.

    The component deliberately creates no OpenVR/OpenXR operator while a
    headset is absent.  It publishes the same eye and sparse hand contracts a
    later runtime adapter will use, so the renderer and interaction graph can
    be accepted on a desktop without pretending compositor validation.
    """

    comp = _ensure(parent, "baseCOMP", "VR_OUTPUT", report)
    _style(comp, -730, 515, (0.34, 0.25, 0.52),
           "Opt-in desktop VR simulation; headset adapter deferred", 285, 125)
    left_eye = _in_top(comp, "LEFT_EYE_IN", 0, report)
    right_eye = _in_top(comp, "RIGHT_EYE_IN", 1, report)

    page = _page(comp, "VR Foundation")
    _custom(comp, page, "Toggle", "Enabled", False,
            label="VR Branch Enabled")
    source = _custom(
        comp, page, "Menu", "Inputsource", "mock",
        ("mock", "openvr"), label="Pose / Hand Provider")
    if source is not None:
        try:
            source.menuLabels = ["Desktop Mock", "Quest / OpenVR (later)"]
        except Exception:
            pass
    _custom(comp, page, "Int", "Targethz", 72,
            label="Target Headset Hz", minimum=60, maximum=144)
    _custom(comp, page, "Int", "Eyewidth", 1280,
            label="Mock Eye Width", minimum=320, maximum=4096)
    _custom(comp, page, "Int", "Eyeheight", 720,
            label="Mock Eye Height", minimum=180, maximum=4096)
    _custom(comp, page, "Float", "Ipdmetres", 0.064,
            label="Mock IPD (metres)", minimum=0.05, maximum=0.08)
    _custom(comp, page, "Float", "Fovdegrees", 75.0,
            label="Mock Vertical FOV", minimum=30.0, maximum=130.0)
    for name, label, default, lower, upper in (
            ("Headxmetres", "Mock Head X (metres)", 0.0, -5.0, 5.0),
            ("Headymetres", "Mock Head Y (metres)", 0.0, -5.0, 5.0),
            ("Headzmetres", "Mock Head Z (metres)", 0.0, -5.0, 5.0),
            ("Headyawdegrees", "Mock Head Yaw", 0.0, -180.0, 180.0),
            ("Headpitchdegrees", "Mock Head Pitch", 0.0, -89.0, 89.0),
            ("Headrolldegrees", "Mock Head Roll", 0.0, -180.0, 180.0)):
        _custom(comp, page, "Float", name, default, label=label,
                minimum=lower, maximum=upper)

    hand_page = _page(comp, "Mock Hands")
    _custom(comp, hand_page, "Toggle", "Handsenabled", False,
            label="Mock Hands Enabled")
    _custom(comp, hand_page, "Float", "Handgain", 0.65,
            label="Hand Interaction Gain", minimum=0.0, maximum=2.0)
    for side, sign in (("Left", -1.0), ("Right", 1.0)):
        _custom(comp, hand_page, "Float", side + "handxmetres", 0.28 * sign,
                label=side + " Hand X", minimum=-3.0, maximum=3.0)
        _custom(comp, hand_page, "Float", side + "handymetres", 0.02,
                label=side + " Hand Y", minimum=-3.0, maximum=3.0)
        _custom(comp, hand_page, "Float", side + "handzmetres", -1.15,
                label=side + " Hand Z", minimum=-5.0, maximum=0.0)
    status = _custom(
        comp, page, "Str", "Status",
        "installation only; mock VR disabled",
        label="VR Runtime Status")
    try:
        status.readOnly = True
    except Exception:
        pass

    disabled_hands = _ensure(comp, "constantTOP", "DISABLED_HANDS", report)
    _set_resolution(disabled_hands, 32, 32)
    _set(disabled_hands, "format", "rgba32float")
    for names in (("colorr", "color1r"), ("colorg", "color1g"),
                  ("colorb", "color1b"), ("colora", "alpha")):
        _set(disabled_hands, names, 0.0)
    mock_hands = _glsl(
        comp, "MOCK_HAND_POSITIONS", "mock_hand_positions",
        [disabled_hands], report, True)
    _set_resolution(mock_hands, 32, 32)
    _set(mock_hands, "format", "rgba32float")
    hands = _ensure(comp, "switchTOP", "HAND_POSITION_ROUTE", report)
    _connect(mock_hands, hands, 0, 0, report, replace=True)
    _connect(disabled_hands, hands, 1, 0, report, replace=True)
    _expr(
        hands, "index",
        "0 if (parent().par.Enabled and parent().par.Inputsource == 'mock' "
        "and parent().par.Handsenabled) else 1")

    _out_top(comp, "OUT_LEFT_EYE", left_eye, 0, report)
    _out_top(comp, "OUT_RIGHT_EYE", right_eye, 1, report)
    _out_top(comp, "OUT_HAND_POSITIONS", hands, 2, report)
    _table(comp, "HEADSET_ADAPTER_CONTRACT", [
        ["field", "contract"],
        ["head_pose", "runtime world_from_head; right-handed Y-up metres"],
        ["eye_pose", "runtime world_from_eye_left/right matrices"],
        ["projection", "runtime left/right projection matrices"],
        ["hands", "two tracked joint sets normalized to world metres"],
        ["submission", "left/right compositor textures plus predicted time"],
        ["current state", "desktop mock only; not headset-validated"],
    ], report)
    _text(
        comp, "README_FIRST",
        "VR FOUNDATION\n\nThis component is intentionally safe without a "
        "headset. Desktop Mock can move only the stereo cameras and publish "
        "two sparse hand primitives into the existing GPU interaction field. "
        "Installation and triple-wall cameras are unchanged. Quest/OpenVR is "
        "a fail-closed future provider: real head pose, per-eye projection, "
        "hand joints, predicted timing and compositor submission must replace "
        "the mock contract and pass a physical Quest 3 acceptance test.",
        report)
    return comp


def _build_sensor(parent, report):
    comp = _ensure(parent, "baseCOMP", "SENSOR_INTERACTION", report)
    _style(comp, -730, 300, (0.18, 0.46, 0.34),
           "Animated fallback sensor; later replace at the same TOP contracts", 245, 110)
    position = _in_top(comp, "WORLD_POSITION_IN", 0, report)
    vr_hands = _in_top(comp, "VR_HAND_POSITION_IN", 1, report)
    page = _page(comp, "Sensor")
    _custom(comp, page, "Menu", "Mode", "simulated",
            ("simulated", "replay", "depth_sensor", "disabled"))
    _custom(comp, page, "Float", "Interactionradius", 0.55,
            label="Interaction Radius (metres)",
            minimum=0.05, maximum=3.0)
    _custom(comp, page, "Float", "Interactionfalloff", 1.0,
            label="Interaction Falloff",
            minimum=0.25, maximum=4.0)
    _custom(comp, page, "Float", "Forcegain", 0.35, label="Force Gain",
            minimum=0.0, maximum=2.0)
    _custom(comp, page, "Float", "Interactionsmoothing", 0.35,
            label="Interaction Smoothing",
            minimum=0.0, maximum=0.92)
    _custom(comp, page, "Float", "Interactionresponse", 0.65,
            label="Interaction Response",
            minimum=0.0, maximum=1.0)
    _custom(comp, page, "Float", "Interactiondecay", 0.5,
            label="Interaction Decay",
            minimum=0.0, maximum=1.0)
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
    _custom(
        sensor_adapter, adapter_page, "Menu", "Sensorsource",
        "depth_anything", ("depth_anything", "femto_mega"),
        label="Audience Sensor Source")
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
    femto_adapter = _build_femto_mega_adapter(
        parent.op("SOURCES"), report)
    depth_anything_bridge = _build_depth_anything_sensor_bridge(
        sensor_adapter, report)
    _wire_depth_anything_sensor_routes(
        sensor_adapter,
        depth_anything_bridge,
        (adapter_position, adapter_mask, adapter_confidence),
        report,
        femto=femto_adapter,
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
    raw_interaction = _glsl(
        comp, "interaction_field", "interaction_field",
        [position, valid_sensor_position, vr_hands], report, False)
    interaction = _build_interaction_smoothing(
        comp, raw_interaction, report)
    interaction_debug = _glsl(
        comp, "INTERACTION_DEBUG", "interaction_debug", [interaction], report, False)
    _out_top(comp, "OUT_SENSOR_POSITION", valid_sensor_position, 0, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 1, report)
    _out_top(comp, "OUT_SENSOR_MASK", mask_switch, 2, report)
    _out_top(comp, "OUT_INTERACTION_DEBUG", interaction_debug, 3, report)
    _text(comp, "CALIBRATION_CONTRACT",
          "DEPTH_SENSOR_ADAPTER/OUT_POSITION must contain sensor-local XYZ "
          "metres in RGB and occupancy in A. OUT_MASK and OUT_CONFIDENCE are "
          "multiplied exactly once after SENSOR_TO_WORLD calibration. Interaction "
          "uses a bounded 32x32 world-space occupancy-primitive search (1024 samples "
          "per generated point), an explicit low-resolution SDF approximation. "
          "OUT_INTERACTION remains signed machine-readable force/occupancy; "
          "The optional VR_HAND_POSITION_IN contract adds exactly two sparse "
          "hand primitives independently from the audience sensor. "
          "OUT_INTERACTION_DEBUG is a display-only color visualization.", report)
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
    _set(state_feedback, ("targettop", "target", "top"), temporal_state.path)

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
    _set(feedback, ("targettop", "target", "top"), persistent.path)
    color_feedback = _ensure(comp, "feedbackTOP", "COLOR_HISTORY", report)
    _connect(color, color_feedback, 0, 0, report, replace=True)
    persistent_color = _glsl(comp, "temporal_color", "temporal_color",
                             [color, color_feedback, temporal_state], report, False)
    _set(color_feedback, ("targettop", "target", "top"), persistent_color.path)
    shader_info = _ensure(comp, "infoDAT", "TEMPORAL_SHADER_INFO", report, optional=True)
    if shader_info is not None:
        _set(shader_info, ("op", "operator"), persistent.path)
    _out_top(comp, "OUT_POSITION", persistent, 0, report)
    _out_top(comp, "OUT_COLOR", persistent_color, 1, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 2, report)
    _out_top(comp, "OUT_TEMPORAL_STATE", temporal_state, 3, report)
    # Newframe is intentionally a one-cook pulse.  Keep both lifecycle
    # branches consuming it even when no wall/window output currently demands
    # a cook, otherwise an idle project can miss accepted frames and age its
    # carried point cloud to zero.
    for name, source in (
            ("POSITION_LIFECYCLE_KEEPALIVE", persistent),
            ("COLOR_LIFECYCLE_KEEPALIVE", persistent_color)):
        keepalive = _ensure(comp, "cacheTOP", name, report)
        _connect(source, keepalive, report=report, replace=True)
        _set(keepalive, "cachesize", 1)
        _set(keepalive, "step", 1)
        _set(keepalive, "alwayscook", True)
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
    geometry_width, geometry_height = _geometry_contract_dimensions(parent)
    _set_resolution(procedural_position, geometry_width, geometry_height)
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
           "Metric point cloud with single, triple-surface and stereo views", 270, 120)
    position = _in_top(comp, "POSITION_IN", 0, report)
    color = _in_top(comp, "COLOR_IN", 1, report)
    interaction = _in_top(comp, "INTERACTION_IN", 2, report)
    page = _page(comp, "Render")
    _custom(comp, page, "Int", "Maxpoints", 120000, label="Maximum Points")
    _custom(comp, page, "Float", "Pointsize", 3.0, label="Point Thickness")
    _custom(comp, page, "Float", "Pointcloudscale", 1.0,
            label="Effective Point Cloud View Scale")
    _custom(comp, page, "Float", "Pointkeep", 0.68,
            label="Visible Point Fraction")
    _custom(comp, page, "Float", "Pointopacity", 0.92,
            label="Point Opacity")
    _custom(comp, page, "Float", "Ipdmetres", 0.064,
            label="Preview Inter-Pupillary Distance (metres)")
    _custom(comp, page, "Toggle", "Vrenabled", False,
            label="Mock VR Camera Enabled")
    _custom(comp, page, "Menu", "Vrinputsource", "mock",
            menu=("mock", "openvr"), label="VR Pose Provider")
    _custom(comp, page, "Float", "Vrfovdegrees", 75.0,
            label="Mock VR Vertical FOV")
    for name in (
            "Vrheadxmetres", "Vrheadymetres", "Vrheadzmetres",
            "Vrheadyawdegrees", "Vrheadpitchdegrees",
            "Vrheadrolldegrees"):
        _custom(comp, page, "Float", name, 0.0,
                label=name.replace("Vrhead", "VR Head "))
    _custom(comp, page, "Float", "Surfacefovdegrees", 60.0,
            label="Artistic Surface Camera FOV (degrees)")
    _custom(comp, page, "Float", "Wrapfovdegrees", 78.0,
            label="Panoramic Surface Camera FOV (degrees)")
    _custom(comp, page, "Float", "Wrapyawdegrees", 30.0,
            label="Panoramic Side Yaw (degrees)")
    _custom(comp, page, "Float", "Artisticyawdegrees", 18.0,
            label="Artistic Side Yaw (degrees)")
    _custom(
        comp, page, "Menu", "Artisticoffsetdirection", "outward",
        menu=("outward", "inward"),
        label="Artistic Side Offset Direction")
    _custom(comp, page, "Float", "Artisticoffsetmetres", 0.45,
            label="Artistic Side Offset (metres)")
    for view, title, enabled_default in (
            ("INSTALLATION", "Installation", True),
            ("LEFT", "Leftwall", False),
            ("CENTER", "Centerwall", True),
            ("RIGHT", "Rightwall", False)):
        _custom(
            comp, page, "Toggle", title + "interactionenabled",
            enabled_default,
            label=title.replace("wall", " Wall") + " Interaction Enabled")
        _custom(
            comp, page, "Float", title + "interactionintensity", 1.0,
            label=title.replace("wall", " Wall") + " Interaction Intensity",
            minimum=0.0, maximum=10.0)

    view_positions = {}
    for view, title, enabled_default in (
            ("INSTALLATION", "Installation", True),
            ("LEFT", "Leftwall", False),
            ("CENTER", "Centerwall", True),
            ("RIGHT", "Rightwall", False)):
        view_position = _glsl(
            comp, "VIEW_POSITION_" + view, "view_interaction",
            [position, interaction], report, True)
        enabled = bool(_value(
            comp, title + "interactionenabled", enabled_default))
        intensity = max(0.0, min(
            10.0, float(_value(
                comp, title + "interactionintensity", 1.0))))
        _patch_shader_float(
            comp.op("VIEW_POSITION_%s_PIXEL" % view),
            "viewInteractionGain", "FLEXGPU_VIEW_INTERACTION_GAIN",
            intensity if enabled else 0.0)
        geometry_width, geometry_height = _geometry_contract_dimensions(parent)
        _set_resolution(view_position, geometry_width, geometry_height)
        view_positions[view] = view_position

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
    triple_renders = {}
    if points is not None:
        _set(points, "rgba", "pactive")
        _set(points, "input0top", view_positions["INSTALLATION"].path)
        _set(points, "input0chanscope", "r g b a")
        # TOP to POP maps one attribute component per sampled channel. Repeating
        # a vector attribute name asks every channel to provide the whole
        # vector, producing "More attribute values than channels specified".
        _set(points, "input0attrscope", "P(0) P(1) P(2) active")
        _set(points, "input0filter", "nearest")
        # Store source color on each point. Using the full source image as the
        # Point Sprite MAT texture makes every point a tiny textured square and
        # visually collapses the cloud back into a dense image plate.
        if _set_sequence_blocks(points, "input", 2):
            _set(points, "input1top", color.path)
            _set(points, "input1chanscope", "r g b a")
            _set(
                points,
                "input1attrscope",
                "Color(0) Color(1) Color(2) Color(3)",
            )
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

        point_sources = {"INSTALLATION": point_source}
        for view in ("LEFT", "CENTER", "RIGHT"):
            branch_points = _ensure(
                comp, "toptoPOP", "POSITION_TO_POINTS_" + view,
                report, optional=True)
            if branch_points is None:
                continue
            _set(branch_points, "rgba", "pactive")
            _set(branch_points, "input0top", view_positions[view].path)
            _set(branch_points, "input0chanscope", "r g b a")
            _set(
                branch_points, "input0attrscope",
                "P(0) P(1) P(2) active")
            _set(branch_points, "input0filter", "nearest")
            if _set_sequence_blocks(branch_points, "input", 2):
                _set(branch_points, "input1top", color.path)
                _set(branch_points, "input1chanscope", "r g b a")
                _set(
                    branch_points, "input1attrscope",
                    "Color(0) Color(1) Color(2) Color(3)")
                _set(branch_points, "input1filter", "nearest")
            _set(branch_points, "surftype", "points")
            _set(branch_points, "texture", "point")
            _set(branch_points, "maxpointsenable", True)
            _expr(branch_points, "maxpoints", "parent().par.Maxpoints")
            branch_source = branch_points
            branch_thin = _ensure(
                comp, "deletePOP", "VISIBLE_POINT_THIN_" + view,
                report, optional=True)
            if branch_thin is not None:
                _connect(
                    branch_points, branch_thin,
                    report=report, replace=True)
                _set(branch_thin, "entity", "point")
                _set(branch_thin, "thinenabled", True)
                _set(branch_thin, "thininvert", False)
                _expr(
                    branch_thin,
                    "thinrandom",
                    "1.0 - pow(max(0.0, 1.0 - "
                    "parent().par.Pointkeep.eval()), 1.0 / 3.0)")
                _set(branch_thin, "thinrandomseed", 19)
                branch_source = branch_thin
            point_sources[view] = branch_source

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

        geometry_by_view = {"INSTALLATION": (geo, selected)}
        for view in ("LEFT", "CENTER", "RIGHT"):
            branch_source = point_sources.get(view)
            if branch_source is None:
                continue
            branch_geo = _ensure(
                comp, "geometryCOMP", "POINT_WORLD_GEO_" + view,
                report, optional=True)
            branch_selected = None
            if branch_geo is not None:
                branch_selected = _ensure(
                    branch_geo, "selectPOP", "SELECT_POINT_WORLD",
                    report, optional=True)
                if branch_selected is not None:
                    _set(branch_selected, "pop", branch_source.path)
                    try:
                        branch_selected.render = True
                        branch_selected.display = True
                    except Exception:
                        pass
                default_primitive = _child(branch_geo, "torus1")
                if default_primitive is not None:
                    try:
                        default_primitive.render = False
                        default_primitive.display = False
                    except Exception:
                        pass
                if point_material is not None:
                    _set(branch_geo, "material", point_material.path)
            geometry_by_view[view] = (branch_geo, branch_selected)

        cameras = {}
        for camera_name, shift_expression in (
            ("CAMERA_CENTER_METRIC", "0.0"),
            ("CAMERA_LEFT_METRIC", "-parent().par.Ipdmetres.eval() * 0.5"),
            ("CAMERA_RIGHT_METRIC", "parent().par.Ipdmetres.eval() * 0.5"),
        ):
            camera = _ensure(comp, "cameraCOMP", camera_name, report,
                             optional=True)
            if camera is not None:
                is_eye = camera_name != "CAMERA_CENTER_METRIC"
                mock_condition = (
                    "parent().par.Vrenabled and "
                    "parent().par.Vrinputsource == 'mock'")
                head_x = (
                    "parent().par.Vrheadxmetres.eval() if (%s) else 0.0"
                    % mock_condition)
                if is_eye:
                    _expr(
                        camera, "tx",
                        "(%s) + (%s)" % (shift_expression, head_x))
                    _expr(
                        camera, "ty",
                        "parent().par.Vrheadymetres.eval() if (%s) else 0.0"
                        % mock_condition)
                    _expr(
                        camera, "tz",
                        "parent().par.Vrheadzmetres.eval() if (%s) else 0.0"
                        % mock_condition)
                    _expr(
                        camera, "rx",
                        "parent().par.Vrheadpitchdegrees.eval() if (%s) else 0.0"
                        % mock_condition)
                    _expr(
                        camera, "ry",
                        "parent().par.Vrheadyawdegrees.eval() if (%s) else 0.0"
                        % mock_condition)
                    _expr(
                        camera, "rz",
                        "parent().par.Vrheadrolldegrees.eval() if (%s) else 0.0"
                        % mock_condition)
                else:
                    _expr(camera, "tx", shift_expression)
                    _set(camera, "ty", 0.0)
                    _set(camera, "tz", 0.0)
                    _set(camera, "rx", 0.0)
                    _set(camera, "ry", 0.0)
                    _set(camera, "rz", 0.0)
                # Match the default 60-degree vertical reconstruction
                # intrinsics so the center camera reprojects the source
                # without an avoidable scale mismatch.
                if is_eye:
                    _expr(
                        camera, "fov",
                        "parent().par.Vrfovdegrees.eval() if (%s) else 60.0"
                        % mock_condition)
                else:
                    _set(camera, "fov", 60.0)
                _set(camera, "near", 0.05)
                _set(camera, "far", 100.0)
                _set(camera, "ipdshift", 0.0)
            cameras[camera_name] = camera

        def make_metric_render(name, camera, view="INSTALLATION"):
            branch_geo, branch_selected = geometry_by_view.get(
                view, (None, None))
            if (branch_geo is None or branch_selected is None or
                    camera is None):
                return None
            node = _ensure(comp, "renderTOP", name, report, optional=True)
            if node is None:
                return None
            _set(node, "geometry", branch_geo.path)
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

        # The panoramic cameras share exactly one origin. Their different yaw
        # directions form a continuous surrounding view when the physical wall
        # angles and camera FOV are calibrated together.
        for side, yaw_expression in (
            # TouchDesigner Camera COMP positive Y rotation looks toward the
            # audience's left when the camera faces -Z.
            ("LEFT", "parent().par.Wrapyawdegrees.eval()"),
            ("CENTER", "0.0"),
            ("RIGHT", "-parent().par.Wrapyawdegrees.eval()"),
        ):
            camera_name = "CAMERA_WRAP_" + side
            camera = _ensure(comp, "cameraCOMP", camera_name, report,
                             optional=True)
            if camera is not None:
                pan_horizontal = (
                    "parent().par.%swallpanhorizontaldegrees.eval()"
                    % side.title())
                pan_vertical = (
                    "parent().par.%swallpanverticaldegrees.eval()"
                    % side.title())
                _set(camera, "tx", 0.0)
                _set(camera, "ty", 0.0)
                _set(camera, "tz", 0.0)
                _expr(camera, "rx", pan_vertical)
                _expr(
                    camera, "ry",
                    "(%s) + (%s)" % (yaw_expression, pan_horizontal))
                _set(camera, "rz", 0.0)
                _expr(camera, "fov", "parent().par.Wrapfovdegrees.eval()")
                _set(camera, "near", 0.05)
                _set(camera, "far", 100.0)
                _set(camera, "ipdshift", 0.0)
            triple_renders["WRAP_" + side] = make_metric_render(
                "METRIC_RENDER_WRAP_" + side, camera, side)

        # Artistic views intentionally move as well as turn the side cameras.
        # Camera translation moves visible content in the opposite screen
        # direction. The default therefore moves the left camera right and the
        # right camera left, pushing both wall images away from the center wall.
        # The public direction menu can restore the older inward screen motion.
        offset_direction = (
            "(1.0 if parent().par.Artisticoffsetdirection.eval() == "
            "'outward' else -1.0)")
        for side, offset_expression, yaw_expression in (
            ("LEFT",
             "parent().par.Artisticoffsetmetres.eval() * " + offset_direction,
             "-parent().par.Artisticyawdegrees.eval()"),
            ("CENTER", "0.0", "0.0"),
            ("RIGHT",
             "-parent().par.Artisticoffsetmetres.eval() * " + offset_direction,
             "parent().par.Artisticyawdegrees.eval()"),
        ):
            camera_name = "CAMERA_ARTISTIC_" + side
            camera = _ensure(comp, "cameraCOMP", camera_name, report,
                             optional=True)
            if camera is not None:
                pan_horizontal = (
                    "parent().par.%swallpanhorizontaldegrees.eval()"
                    % side.title())
                pan_vertical = (
                    "parent().par.%swallpanverticaldegrees.eval()"
                    % side.title())
                _expr(camera, "tx", offset_expression)
                _set(camera, "ty", 0.0)
                _set(camera, "tz", 0.0)
                _expr(camera, "rx", pan_vertical)
                _expr(
                    camera, "ry",
                    "(%s) + (%s)" % (yaw_expression, pan_horizontal))
                _set(camera, "rz", 0.0)
                _expr(camera, "fov", "parent().par.Surfacefovdegrees.eval()")
                _set(camera, "near", 0.05)
                _set(camera, "far", 100.0)
                _set(camera, "ipdshift", 0.0)
            triple_renders["ARTISTIC_" + side] = make_metric_render(
                "METRIC_RENDER_ARTISTIC_" + side, camera, side)

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
                _set(legacy, "fov", 60.0)
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
            for mode in ("WRAP", "ARTISTIC"):
                for side in ("LEFT", "CENTER", "RIGHT"):
                    triple_renders[mode + "_" + side] = legacy

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
    output_index = 3
    for mode in ("WRAP", "ARTISTIC"):
        for side in ("LEFT", "CENTER", "RIGHT"):
            key = mode + "_" + side
            switch = _ensure(
                comp, "switchTOP", key + "_OR_FALLBACK", report)
            _connect(color, switch, 0, 0, report, replace=True)
            rendered = triple_renders.get(key)
            if rendered is not None:
                _connect(rendered, switch, 1, 0, report, replace=True)
                _set(switch, "index", 1)
            else:
                _set(switch, "index", 0)
            _out_top(comp, "OUT_" + key, switch, output_index, report)
            output_index += 1
    _table(comp, "RENDER_PATH", [
        ["stage", "operator", "contract"],
        ["unpack", "POSITION_TO_POINTS (TOP to POP)", "P + active plus aligned per-point Color"],
        ["spacing", "VISIBLE_POINT_THIN (Delete POP)", "stable random keep; 68% default"],
        ["glyph", "POINT_GLYPH + POINT_SPRITE_MATERIAL", "soft circular alpha; no source-image sprite texture"],
        ["thickness", "POINT_SPRITE_MATERIAL", "Pointsize and Pointopacity controls"],
        ["metric geometry", "POINT_WORLD_GEO/SELECT_POINT_WORLD", "no normalization; XYZ remain metres"],
        ["view scale", "managed Camera COMP FOV", "perspective-correct provider-aware framing; geometry stays metric"],
        ["center", "METRIC_RENDER_CENTER + CAMERA_CENTER_METRIC", TOP_CONTRACTS["INSTALLATION"]],
        ["panoramic wrap", "METRIC_RENDER_WRAP_* + CAMERA_WRAP_*",
         "shared origin; side yaw follows Wrapyawdegrees"],
        ["artistic multi-angle", "METRIC_RENDER_ARTISTIC_* + CAMERA_ARTISTIC_*",
         "side translation plus toe-in; deliberate parallax"],
        ["stereo", "METRIC_RENDER_LEFT/RIGHT + parallel Camera COMPs", "+/- Ipdmetres/2; no toe-in"],
        ["fallback", "*_OR_FALLBACK input 0", "completed color TOP"],
    ], report)
    _apply_point_cloud_camera_framing(comp)
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


def _build_triple_display(parent, report):
    comp = _ensure(parent, "baseCOMP", "TRIPLE_DISPLAY", report)
    _style(comp, 690, 520, (0.43, 0.42, 0.12),
           "Three-surface panoramic wrap and artistic multi-angle outputs", 285, 125)
    page = _page(comp, "View Completion")
    _custom(comp, page, "Float", "Fogdensity", 0.35,
            label="Per-Surface Fog Density")
    _custom(comp, page, "Float", "Fogradius", 2.0,
            label="Per-Surface Fog Radius")
    _custom(comp, page, "Float", "Wrapcoverage", 0.55,
            label="Panoramic Procedural Coverage")
    _custom(comp, page, "Float", "Wrapnoise", 0.42,
            label="Panoramic Coverage Noise")

    inputs = {}
    input_index = 0
    for mode in ("WRAP", "ARTISTIC"):
        for side in ("LEFT", "CENTER", "RIGHT"):
            key = mode + "_" + side
            inputs[key] = _in_top(
                comp, key + "_IN", input_index, report)
            input_index += 1
    fog_plate = _in_top(comp, "FOG_PLATE_IN", input_index, report)

    graded = {}
    coverage = {}
    output_index = 0
    layouts = {}
    for mode in ("WRAP", "ARTISTIC"):
        for side_index, side in enumerate(("LEFT", "CENTER", "RIGHT")):
            key = mode + "_" + side
            grade_input = inputs[key]
            if mode == "WRAP":
                coverage[key] = _glsl(
                    comp, "COVERAGE_" + key, "panoramic_coverage",
                    [inputs[key]], report, False)
                coverage_source = comp.op("COVERAGE_" + key + "_PIXEL")
                _patch_shader_float(
                    coverage_source, "wrapCoverage",
                    "FLEXGPU_WRAP_COVERAGE",
                    _value(comp, "Wrapcoverage", 0.55))
                _patch_shader_float(
                    coverage_source, "wrapNoise",
                    "FLEXGPU_WRAP_NOISE",
                    _value(comp, "Wrapnoise", 0.42))
                _patch_shader_float(
                    coverage_source, "wrapPanelIndex",
                    "FLEXGPU_WRAP_PANEL_INDEX", float(side_index))
                _set_resolution(coverage[key], 960, 540)
                grade_input = coverage[key]
            graded[key] = _glsl(
                comp, "GRADE_" + key, "installation_grade",
                [grade_input, fog_plate], report, False)
            grade_source = comp.op("GRADE_" + key + "_PIXEL")
            _patch_shader_float(
                grade_source, "viewFogDensity",
                "FLEXGPU_VIEW_FOG_DENSITY",
                _value(comp, "Fogdensity", 0.35))
            _patch_shader_float(
                grade_source, "viewFogRadius",
                "FLEXGPU_VIEW_FOG_RADIUS",
                _value(comp, "Fogradius", 2.0))
            _set_resolution(graded[key], 960, 540)
            _out_top(comp, "OUT_" + key, graded[key], output_index, report)
            output_index += 1

        layout = _ensure(
            comp, "layoutTOP", mode + "_MOSAIC", report, optional=True)
        if layout is None:
            layout = _ensure(
                comp, "compositeTOP", mode + "_MOSAIC_FALLBACK", report)
        for side_index, side in enumerate(("LEFT", "CENTER", "RIGHT")):
            _connect(
                graded[mode + "_" + side], layout, side_index, 0,
                report, replace=True)
        _set_horizontal_layout(layout)
        _set_resolution(layout, 2880, 540)
        layouts[mode] = layout
        _out_top(comp, "OUT_" + mode + "_MOSAIC", layout, output_index, report)
        output_index += 1

    _table(comp, "SURFACE_CONTRACT", [
        ["output", "camera relationship", "use"],
        ["OUT_WRAP_LEFT/CENTER/RIGHT", "one origin; TD camera yaw +A / 0 / -A",
         "calibrated surrounding walls with continuous perspective"],
        ["OUT_WRAP_MOSAIC", "left | center | right",
         "single-canvas preview, recorder, or projector mapper input"],
        ["OUT_ARTISTIC_LEFT/CENTER/RIGHT", "translated and rotated cameras",
         "sculptural multi-angle presentation with intentional seams"],
        ["OUT_ARTISTIC_MOSAIC", "left | center | right",
         "single-canvas preview, recorder, or projector mapper input"],
    ], report)
    _text(comp, "README_FIRST",
          "Each surface TOP is an independent projector/LED feed. The mosaic "
          "TOPs concatenate left, center and right only for preview, recording "
          "or a downstream mapping tool. Panoramic Wrap requires venue camera "
          "yaw/FOV calibration. Artistic Multi-Angle deliberately does not "
          "promise continuous seams.", report)
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
    _set_horizontal_layout(layout)
    _set_resolution(layout, 2560, 720)
    _out_top(comp, "OUT_LEFT_EYE", left_grade, 0, report)
    _out_top(comp, "OUT_RIGHT_EYE", right_grade, 1, report)
    _out_top(comp, "OUT_STEREO_SBS", layout, 2, report)

    # Some merged working TOEs retain a cooked 128x128 texture on their
    # original In/GLSL/Out TOP chain even after the external component inputs
    # and Common-page resolution parameters are repaired.  Keep those legacy
    # operators as rollback evidence, but expose a fresh managed path that
    # selects the stable point-render eyes directly.  The repair GLSL TOPs
    # intentionally share the original pixel DATs so Show Control color/fog
    # adjustments continue to affect the public stereo outputs.
    left_source = _ensure(
        comp, "selectTOP", "LEFT_SOURCE_REPAIR", report)
    right_source = _ensure(
        comp, "selectTOP", "RIGHT_SOURCE_REPAIR", report)
    _set(left_source, ("top", "topselect"),
         "../POINT_RENDER/OUT_LEFT_EYE")
    _set(right_source, ("top", "topselect"),
         "../POINT_RENDER/OUT_RIGHT_EYE")
    left_grade_repair = _ensure(
        comp, "glslTOP", "GRADE_LEFT_EYE_REPAIR", report)
    right_grade_repair = _ensure(
        comp, "glslTOP", "GRADE_RIGHT_EYE_REPAIR", report)
    for repair, source, pixel_dat in (
            (left_grade_repair, left_source,
             comp.op("GRADE_LEFT_EYE_PIXEL")),
            (right_grade_repair, right_source,
             comp.op("GRADE_RIGHT_EYE_PIXEL"))):
        _set(repair, ("pixeldat", "pixelshader"), pixel_dat.path)
        _set(repair, "outputresolution", "useinput")
        _set(repair, "format", "rgba16float")
        _connect(source, repair, report=report, replace=True)
    layout_repair = _ensure(
        comp, "layoutTOP", "STEREO_SIDE_BY_SIDE_REPAIR", report)
    _connect(
        left_grade_repair, layout_repair, 0, 0, report, replace=True)
    _connect(
        right_grade_repair, layout_repair, 1, 0, report, replace=True)
    _set_horizontal_layout(layout_repair)
    _set(layout_repair, "outputresolution", "useinput")

    # Connect Order defines the Base COMP output slots in TouchDesigner 2025.
    # Move the stale legacy outputs aside without destroying them and publish
    # the fresh graded path at the established 0/1/2 contract.
    for legacy, index in (
            (comp.op("OUT_LEFT_EYE"), 3),
            (comp.op("OUT_RIGHT_EYE"), 4),
            (comp.op("OUT_STEREO_SBS"), 5)):
        _set(legacy, ("connectorder", "outputindex", "index"), index)
    _out_top(
        comp, "OUT_LEFT_EYE_REPAIR", left_grade_repair, 0, report)
    _out_top(
        comp, "OUT_RIGHT_EYE_REPAIR", right_grade_repair, 1, report)
    _out_top(
        comp, "OUT_STEREO_SBS_REPAIR", layout_repair, 2, report)
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


def _interaction_world_position_source(pipeline):
    """Return the public live position contract used by the active renderer."""

    if pipeline is None:
        return None
    source = pipeline.op("OUT_POSITION")
    if source is not None:
        return source
    contract = pipeline.op("RENDER_CONTRACT")
    if contract is not None:
        return contract
    # Compatibility fallback for older, unmerged project revisions.
    return pipeline.op("RECONSTRUCTION")


def _install_interaction_debug_output(sensor, pipeline, report):
    """Add the display-only interaction view without rebuilding other stages."""

    if sensor is None or pipeline is None:
        raise RuntimeError("interaction debug requires the existing sensor and pipeline")
    interaction = sensor.op("interaction_field")
    if interaction is None:
        raise RuntimeError("interaction_field is missing; build the working pipeline first")
    interaction_debug = _glsl(
        sensor, "INTERACTION_DEBUG", "interaction_debug", [interaction], report, False)
    _out_top(sensor, "OUT_INTERACTION_DEBUG", interaction_debug, 3, report)
    root_output = _ensure(pipeline, "nullTOP", "OUT_INTERACTION_DEBUG", report)
    _connect(sensor, root_output, 0, 3, report, replace=True)
    _style(
        root_output, 1030, -1160, (0.18, 0.50, 0.28),
        "OUT_INTERACTION_DEBUG", 185, 70)
    return interaction_debug


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
    sensor = root.op("WORKING_PIPELINE/SENSOR_INTERACTION")
    pipeline = root.op("WORKING_PIPELINE")
    _install_interaction_debug_output(sensor, pipeline, report)
    try:
        bridge.store("depth_anything_bridge_install_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] Depth Anything sensor bridge installed disabled: %s "
          "(%d created, %d reused, %d warnings)" %
          (bridge.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return bridge


def install_femto_mega_sensor_bridge(root=None):
    """Add a selectable native Femto Mega source to an existing local TOE.

    This bounded installer reuses the current adapter fallbacks, keeps
    ``depth_anything`` as the default source, creates only the public native
    Orbbec adapter plus managed route selectors, and refreshes Show Control.
    It never changes webcam name/index/mirror settings, starts a worker, embeds
    a device serial, inspects private components, or saves the current TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    sources = pipeline.op("SOURCES") if pipeline is not None else None
    sensor = (
        pipeline.op("SENSOR_INTERACTION") if pipeline is not None else None)
    adapter = (
        sensor.op("DEPTH_SENSOR_ADAPTER") if sensor is not None else None)
    if pipeline is None or sources is None or sensor is None or adapter is None:
        raise RuntimeError(
            "managed sensor pipeline is missing; build WORKING_PIPELINE first")

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

    femto = _build_femto_mega_adapter(sources, report)
    bridge = _build_depth_anything_sensor_bridge(adapter, report)
    _wire_depth_anything_sensor_routes(
        adapter, bridge, tuple(fallbacks), report, femto=femto)
    control = _build_show_control(pipeline, report)
    world_position = _interaction_world_position_source(pipeline)
    if world_position is None:
        raise RuntimeError("managed live position contract is missing")
    _connect(world_position, sensor, 0, 0, report, replace=True)
    _apply_sensor_calibration_shader_values(pipeline, control)
    _apply_femto_depth_gate_shader_values(pipeline, control)
    _install_interaction_debug_output(sensor, pipeline, report)
    try:
        femto.store("femto_mega_install_report", report.as_dict())
    except Exception:
        pass
    print(
        "[FlexGPU runtime] Femto Mega source ready default-off: %s; "
        "webcam + Depth Anything preserved; TOE remains unsaved "
        "(%d created, %d reused, %d warnings)" %
        (femto.path, len(report.created), len(report.reused),
         len(report.warnings)))
    return femto


def install_depth_anything_geometry_bridge(root=None):
    """Install only the alternative generated-image Depth Anything branch.

    This does not touch the webcam/audience sensor bridge. It reuses the
    adapter's current generated RGB input, adds isolated ports and routes, and
    leaves ``Geometrysource`` on its existing value (MoGe by default).
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
        raise RuntimeError(
            "StreamDiffusion adapter is missing; build the working pipeline first")

    existing = adapter.op(
        "DEPTH_ANYTHING_GEOMETRY_BRIDGE/bridge_runtime")
    if existing is not None:
        try:
            existing.module.stop(
                adapter.op("DEPTH_ANYTHING_GEOMETRY_BRIDGE"))
        except Exception:
            pass

    page = _page(adapter, "Adapter")
    _custom(adapter, page, "Menu", "Geometrysource", "moge2",
            ("moge2", "depth_anything"), label="Generated Geometry Source")
    moge2_routes = tuple(adapter.op(name) for name in (
        "MOGE2_RGB_ROUTE", "MOGE2_DEPTH_ROUTE",
        "MOGE2_CONFIDENCE_ROUTE", "MOGE2_MASK_ROUTE"))
    if any(route is None for route in moge2_routes):
        raise RuntimeError(
            "MoGe route boundary is missing; install the MoGe bridge first")
    input_rgb = _first_input(adapter.op("MOGE2_BRIDGE"))
    if input_rgb is None:
        input_rgb = _first_input(moge2_routes[0])
    if input_rgb is None:
        input_rgb = adapter.op("REPLACE_WITH_STREAMDIFFUSION_RGB")
    if input_rgb is None:
        raise RuntimeError("could not preserve the generated RGB source")

    bridge = _build_depth_anything_geometry_bridge(adapter, input_rgb, report)
    _wire_generated_geometry_routes(adapter, bridge, moge2_routes, report)
    try:
        bridge.store(
            "depth_anything_geometry_install_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] Depth Anything geometry bridge installed disabled: "
          "%s (%d created, %d reused, %d warnings)" %
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
        ("OUT_RGB", "GENERATED_GEOMETRY_RGB_ROUTE", "MOGE2_RGB_ROUTE",
         "REPLACE_WITH_STREAMDIFFUSION_RGB"),
        ("OUT_DEPTH", "GENERATED_GEOMETRY_DEPTH_ROUTE", "MOGE2_DEPTH_ROUTE",
         "REPLACE_WITH_DEPTH_ESTIMATE"),
        ("OUT_CONFIDENCE", "GENERATED_GEOMETRY_CONFIDENCE_ROUTE",
         "MOGE2_CONFIDENCE_ROUTE", "REPLACE_WITH_CONFIDENCE"),
        ("OUT_MASK", "GENERATED_GEOMETRY_MASK_ROUTE", "MOGE2_MASK_ROUTE",
         "REPLACE_WITH_VALID_MASK"),
    )
    fallbacks = []
    for output_name, selector_name, route_name, placeholder_name in fallback_names:
        output = adapter.op(output_name)
        source = _first_input(output)
        if source is not None and str(getattr(source, "name", "")) == selector_name:
            source = _first_input(source)
        if source is not None and str(getattr(source, "name", "")) == route_name:
            source = _first_input(source)
        if source is None:
            source = adapter.op(placeholder_name)
        if source is None:
            raise RuntimeError("could not preserve fallback source for " + output_name)
        fallbacks.append(source)

    bridge = _build_moge2_bridge(adapter, fallbacks[0], report)
    routes = _wire_moge2_routes(adapter, bridge, tuple(fallbacks), report)
    depth_anything = adapter.op("DEPTH_ANYTHING_GEOMETRY_BRIDGE")
    if depth_anything is not None:
        page = _page(adapter, "Adapter")
        _custom(adapter, page, "Menu", "Geometrysource", "moge2",
                ("moge2", "depth_anything"),
                label="Generated Geometry Source")
        _wire_generated_geometry_routes(
            adapter, depth_anything, routes, report)
    try:
        bridge.store("moge2_bridge_install_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] MoGe-2 bridge installed disabled: %s "
          "(%d created, %d reused, %d warnings)" %
          (bridge.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return bridge


def _build_show_control(pipeline, report):
    """Create one public control surface without owning private components."""

    control = _ensure(pipeline, "baseCOMP", "SHOW_CONTROL", report)
    _style(
        control, -1080, 640, (0.52, 0.28, 0.16),
        "Live show controls: source, display, completion, interaction and quality",
        310, 155)
    adapter = pipeline.op("SOURCES/STREAMDIFFUSION_ADAPTER")
    completion = pipeline.op("COMPLETION")
    sensor = pipeline.op("SENSOR_INTERACTION")
    render = pipeline.op("POINT_RENDER")
    triple = pipeline.op("TRIPLE_DISPLAY")
    reference_wall = pipeline.op("INSTALLATION_OUTPUT/installation_grade")
    wall_width = int(_value(
        reference_wall, ("resolutionw", "resw"), 1920))
    wall_height = int(_value(
        reference_wall, ("resolutionh", "resh"), 1080))
    _ensure_audio_adapter_contract(adapter)

    show_page = _page(control, "Show Controls")
    _custom(
        control, show_page, "Menu", "Geometryprovider",
        _value(adapter, "Geometrysource", "moge2"),
        ("moge2", "depth_anything"), label="Geometry Provider")
    _custom(
        control, show_page, "Menu", "Displaymode",
        _value(pipeline, "Displaymode", "single"),
        ("single", "panoramic_wrap", "artistic_multi_angle"),
        label="Display Mode")
    _custom(
        control, show_page, "Int", "Wallwidth", wall_width,
        label="Wall Width")
    _custom(
        control, show_page, "Int", "Wallheight", wall_height,
        label="Wall Height")
    _custom(
        control, show_page, "Menu", "Completionmode",
        _value(completion, "Mode", "hybrid"),
        ("fog", "procedural", "hybrid"), label="Completion Mode")
    _custom(
        control, show_page, "Float", "Fogdensity",
        _value(completion, "Fogdensity", 0.35), label="Fog Density")
    _custom(
        control, show_page, "Float", "Interactionstrength",
        _value(sensor, "Forcegain", 0.35), label="Interaction Strength",
        minimum=0.0, maximum=2.0)
    _custom(
        control, show_page, "Float", "Interactionsmoothing",
        _value(sensor, "Interactionsmoothing", 0.35),
        label="Interaction Smoothing",
        minimum=0.0, maximum=0.92)
    _custom(
        control, show_page, "Float", "Wrapyawdegrees",
        _value(render, "Wrapyawdegrees", 30.0),
        label="Panoramic Side Yaw")
    _custom(
        control, show_page, "Float", "Wrapfovdegrees",
        _value(render, "Wrapfovdegrees", 78.0),
        label="Panoramic Surface FOV")
    _custom(
        control, show_page, "Float", "Wrapcoverage",
        _value(triple, "Wrapcoverage", 0.55),
        label="Panoramic Coverage")
    _custom(
        control, show_page, "Float", "Wrapnoise",
        _value(triple, "Wrapnoise", 0.42),
        label="Panoramic Coverage Noise")
    _custom(
        control, show_page, "Float", "Surfacefovdegrees",
        _value(render, "Surfacefovdegrees", 60.0),
        label="Artistic Surface FOV")
    _custom(
        control, show_page, "Float", "Artisticyawdegrees",
        _value(render, "Artisticyawdegrees", 18.0),
        label="Artistic Side Yaw")
    _custom(
        control, show_page, "Menu", "Artisticoffsetdirection",
        _value(render, "Artisticoffsetdirection", "outward"),
        ("outward", "inward"),
        label="Artistic Offset Direction")
    _custom(
        control, show_page, "Float", "Artisticoffsetmetres",
        _value(render, "Artisticoffsetmetres", 0.45),
        label="Artistic Side Offset (metres)")
    for side in ("Left", "Center", "Right"):
        parameter = side + "wallscale"
        _custom(
            control, show_page, "Float", parameter,
            _value(render, parameter, 1.0),
            label=side + " Wall View Scale")
        horizontal = side + "wallpanhorizontaldegrees"
        vertical = side + "wallpanverticaldegrees"
        _custom(
            control, show_page, "Float", horizontal,
            _value(render, horizontal, 0.0),
            label=side + " Camera Horizontal Pan")
        _custom(
            control, show_page, "Float", vertical,
            _value(render, vertical, 0.0),
            label=side + " Camera Vertical Pan")
    _custom(
        control, show_page, "Float", "Pointcloudscale", 1.0,
        label="Point Cloud Scale")
    _custom(
        control, show_page, "Float", "Moge2scale", 1.25,
        label="MoGe-2 Scale")
    _custom(
        control, show_page, "Float", "Depthanythingscale", 1.0,
        label="Depth Anything Scale")
    provider = str(_value(adapter, "Geometrysource", "moge2"))
    provider_scale = 1.0 if provider == "depth_anything" else 1.25
    effective_scale = _custom(
        control, show_page, "Float", "Effectivepointcloudscale",
        provider_scale, label="Effective Point Cloud Scale")
    try:
        effective_scale.readOnly = True
    except Exception:
        pass
    creative_scale = max(
        0.5, min(2.5, float(_value(control, "Pointcloudscale", 1.0))))
    active_provider_scale = max(
        0.5, min(2.5, float(_value(
            control,
            "Depthanythingscale" if provider == "depth_anything"
            else "Moge2scale",
            provider_scale))))
    active_scale = max(0.35, min(4.0,
                                 creative_scale * active_provider_scale))
    _set(render, "Pointcloudscale", active_scale)
    _set(control, "Effectivepointcloudscale", active_scale)
    _custom(control, show_page, "Pulse", "Applyall", False,
            label="Apply All Show Controls")

    routing_page = _page(control, "Interaction Routing")
    _custom(
        control, routing_page, "Float", "Interactionradius",
        _value(sensor, "Interactionradius", 0.55),
        label="Interaction Radius (metres)",
        minimum=0.05, maximum=3.0)
    _custom(
        control, routing_page, "Float", "Interactionfalloff",
        _value(sensor, "Interactionfalloff", 1.0),
        label="Interaction Edge Falloff",
        minimum=0.25, maximum=4.0)
    _custom(
        control, routing_page, "Float", "Interactionresponse",
        _value(sensor, "Interactionresponse", 0.65),
        label="Interaction Response",
        minimum=0.0, maximum=1.0)
    _custom(
        control, routing_page, "Float", "Interactiondecay",
        _value(sensor, "Interactiondecay", 0.5),
        label="Interaction Decay",
        minimum=0.0, maximum=1.0)
    for title, enabled_default in (
            ("Installation", True),
            ("Leftwall", False),
            ("Centerwall", True),
            ("Rightwall", False)):
        readable = title.replace("wall", " Wall")
        _custom(
            control, routing_page, "Toggle",
            title + "interactionenabled",
            bool(_value(
                render, title + "interactionenabled", enabled_default)),
            label=readable + " Interaction Enabled")
        _custom(
            control, routing_page, "Float",
            title + "interactionintensity",
            float(_value(
                render, title + "interactionintensity", 1.0)),
            label=readable + " Interaction Intensity",
            minimum=0.0, maximum=10.0)

    vr_page = _page(control, "VR Simulation")
    experience = _custom(
        control, vr_page, "Menu", "Experience", "installation",
        ("installation", "vr", "combined"), label="Experience Mode")
    if experience is not None:
        try:
            experience.menuLabels = [
                "Installation Only",
                "VR Only (desktop simulation)",
                "Installation + VR",
            ]
        except Exception:
            pass
    vr_source = _custom(
        control, vr_page, "Menu", "Vrinputsource", "mock",
        ("mock", "openvr"), label="VR Pose / Hand Provider")
    if vr_source is not None:
        try:
            vr_source.menuLabels = [
                "Desktop Mock",
                "Quest / OpenVR (requires headset)",
            ]
        except Exception:
            pass
    _custom(control, vr_page, "Int", "Vrtargethz", 72,
            label="Target Headset Hz", minimum=60, maximum=144)
    _custom(control, vr_page, "Int", "Vreyewidth", 1280,
            label="Mock Eye Width", minimum=320, maximum=4096)
    _custom(control, vr_page, "Int", "Vreyeheight", 720,
            label="Mock Eye Height", minimum=180, maximum=4096)
    _custom(control, vr_page, "Float", "Vripdmetres", 0.064,
            label="Mock IPD (metres)", minimum=0.05, maximum=0.08)
    _custom(control, vr_page, "Float", "Vrfovdegrees", 75.0,
            label="Mock Vertical FOV", minimum=30.0, maximum=130.0)
    for name, label, lower, upper in (
            ("Vrheadxmetres", "Mock Head X (metres)", -5.0, 5.0),
            ("Vrheadymetres", "Mock Head Y (metres)", -5.0, 5.0),
            ("Vrheadzmetres", "Mock Head Z (metres)", -5.0, 5.0),
            ("Vrheadyawdegrees", "Mock Head Yaw", -180.0, 180.0),
            ("Vrheadpitchdegrees", "Mock Head Pitch", -89.0, 89.0),
            ("Vrheadrolldegrees", "Mock Head Roll", -180.0, 180.0)):
        _custom(control, vr_page, "Float", name, 0.0, label=label,
                minimum=lower, maximum=upper)
    _custom(control, vr_page, "Pulse", "Resetvrheadpose", False,
            label="Reset Mock Head Pose")

    vr_hands_page = _page(control, "VR Mock Hands")
    _custom(control, vr_hands_page, "Toggle", "Vrhandenabled", False,
            label="Mock Hand Interaction Enabled")
    _custom(control, vr_hands_page, "Float", "Vrhandgain", 0.65,
            label="Hand Interaction Gain", minimum=0.0, maximum=2.0)
    for side, sign in (("Left", -1.0), ("Right", 1.0)):
        prefix = "Vr%shand" % side.lower()
        _custom(control, vr_hands_page, "Float", prefix + "xmetres",
                0.28 * sign, label=side + " Hand X",
                minimum=-3.0, maximum=3.0)
        _custom(control, vr_hands_page, "Float", prefix + "ymetres",
                0.02, label=side + " Hand Y",
                minimum=-3.0, maximum=3.0)
        _custom(control, vr_hands_page, "Float", prefix + "zmetres",
                -1.15, label=side + " Hand Z",
                minimum=-5.0, maximum=0.0)
    _custom(control, vr_hands_page, "Pulse", "Resetvrhands", False,
            label="Reset Mock Hands")
    vr_status = _custom(
        control, vr_page, "Str", "Vrstatus",
        "installation only; mock VR disabled",
        label="VR Runtime Status")
    try:
        vr_status.readOnly = True
    except Exception:
        pass

    audio_page = _page(control, "Audio")
    _custom(
        control, audio_page, "Toggle", "Audioenabled",
        bool(_value(adapter, "Audioenabled", False)),
        label="Audio Enabled")
    audio_source = _custom(
        control, audio_page, "Menu", "Audiosource",
        str(_value(adapter, "Audiosource", "voices")),
        ("voices", "soundscape"), label="Audio Source")
    if audio_source is not None:
        try:
            audio_source.menuLabels = [
                "Human Voices Only",
                "Soundscape Only",
            ]
        except Exception:
            pass

    color_page = _page(control, "Color Adjustment")
    _custom(
        control, color_page, "Float", "Brightness", 0.0,
        label="Brightness", minimum=-1.0, maximum=1.0)
    _custom(
        control, color_page, "Float", "Contrast", 1.0,
        label="Contrast", minimum=0.0, maximum=3.0)
    _custom(
        control, color_page, "Float", "Saturation", 1.0,
        label="Saturation", minimum=0.0, maximum=3.0)
    _custom(
        control, color_page, "Float", "Gamma", 1.0,
        label="Gamma", minimum=0.2, maximum=3.0)
    _custom(
        control, color_page, "Float", "Hueshiftdegrees", 0.0,
        label="Hue Shift (degrees)", minimum=-180.0, maximum=180.0)
    _custom(
        control, color_page, "Float", "Temperature", 0.0,
        label="Temperature", minimum=-1.0, maximum=1.0)
    _custom(
        control, color_page, "Float", "Tint", 0.0,
        label="Tint", minimum=-1.0, maximum=1.0)
    _custom(
        control, color_page, "Pulse", "Resetcolor", False,
        label="Reset Color Adjustment")

    camera_page = _page(control, "Camera Depth")
    sensor_adapter = (
        sensor.op("DEPTH_SENSOR_ADAPTER") if sensor is not None else None)
    sensor_bridge = (
        sensor_adapter.op("DEPTH_ANYTHING_BRIDGE")
        if sensor_adapter is not None else None)
    femto_adapter = pipeline.op("SOURCES/FEMTO_MEGA_ADAPTER")
    sensor_enabled = (
        str(_value(sensor, "Mode", "disabled")) == "depth_sensor" and
        bool(_value(sensor_adapter, "Enabled", False)))
    sensor_source = _custom(
        control, camera_page, "Menu", "Camerasensorsource",
        str(_value(sensor_adapter, "Sensorsource", "depth_anything")),
        ("depth_anything", "femto_mega"), label="Camera Depth Source")
    if sensor_source is not None:
        try:
            sensor_source.menuLabels = [
                "Webcam + Depth Anything",
                "Femto Mega (USB)",
            ]
        except Exception:
            pass
    _custom(
        control, camera_page, "Toggle", "Camerainteractionenabled",
        sensor_enabled, label="Camera Interaction Enabled")
    _custom(
        control, camera_page, "Str", "Cameraname", "",
        label="Camera Device Name (optional)")
    _custom(
        control, camera_page, "Int", "Cameraindex", 0,
        label="Camera Index (fallback)", minimum=0, maximum=31)
    _custom(
        control, camera_page, "Toggle", "Cameramirrorhorizontal",
        bool(_value(sensor_bridge, "Mirrorhorizontal", True)),
        label="Webcam Mirror Horizontal")
    _custom(
        control, camera_page, "Str", "Femtodeviceserial",
        str(_value(femto_adapter, "Deviceserial", "")),
        label="Femto Mega Serial (optional)")
    _custom(
        control, camera_page, "Pulse", "Startcameradepthworker", False,
        label="Start Webcam Depth Worker")
    _custom(
        control, camera_page, "Pulse", "Stopcameradepthworker", False,
        label="Stop Webcam Depth Worker")
    sensor_worker_pid = _custom(
        control, camera_page, "Int", "Sensorworkerpid", 0,
        label="Camera Worker PID")
    sensor_worker_status = _custom(
        control, camera_page, "Str", "Sensorworkerstatus",
        "idle; camera RGB remains inside the local worker",
        label="Camera Worker Status")
    femto_status = _custom(
        control, camera_page, "Str", "Femtostatus",
        "inactive; webcam + Depth Anything settings are preserved",
        label="Femto Mega Status")
    for parameter in (
            sensor_worker_pid, sensor_worker_status, femto_status):
        try:
            parameter.readOnly = True
        except Exception:
            pass

    calibration_page = _page(control, "Camera Calibration")
    _custom(
        control, calibration_page, "Float", "Sensorpositionscale", 1.0,
        label="Webcam Depth Position Scale",
        minimum=0.25, maximum=4.0)
    for axis in ("X", "Y", "Z"):
        _custom(
            control, calibration_page, "Float",
            "Sensortrim%smetres" % axis.lower(), 0.0,
            label="Webcam World %s Trim (metres)" % axis,
            minimum=-5.0, maximum=5.0)
    _custom(
        control, calibration_page, "Float",
        "Sensortrimyawdegrees", 0.0,
        label="Webcam Sensor Yaw Trim (degrees)",
        minimum=-180.0, maximum=180.0)
    _custom(
        control, calibration_page, "Float",
        "Sensortrimpitchdegrees", 0.0,
        label="Webcam Sensor Pitch Trim (degrees)",
        minimum=-90.0, maximum=90.0)
    _custom(
        control, calibration_page, "Float",
        "Sensortrimrolldegrees", 0.0,
        label="Webcam Sensor Roll Trim (degrees)",
        minimum=-180.0, maximum=180.0)
    _custom(
        control, calibration_page, "Pulse",
        "Resetsensorcalibrationtrim", False,
        label="Reset Webcam Calibration Trim")
    _custom(
        control, calibration_page, "Float", "Femtopositionscale", 1.0,
        label="Femto Depth Position Scale",
        minimum=0.25, maximum=4.0)
    _custom(
        control, calibration_page, "Toggle", "Femtomirrorhorizontal", False,
        label="Femto Mirror Horizontal")
    for axis in ("X", "Y", "Z"):
        _custom(
            control, calibration_page, "Float",
            "Femtotrim%smetres" % axis.lower(), 0.0,
            label="Femto World %s Trim (metres)" % axis,
            minimum=-5.0, maximum=5.0)
    _custom(
        control, calibration_page, "Float",
        "Femtotrimyawdegrees", 0.0,
        label="Femto Sensor Yaw Trim (degrees)",
        minimum=-180.0, maximum=180.0)
    _custom(
        control, calibration_page, "Float",
        "Femtotrimpitchdegrees", 0.0,
        label="Femto Sensor Pitch Trim (degrees)",
        minimum=-90.0, maximum=90.0)
    _custom(
        control, calibration_page, "Float",
        "Femtotrimrolldegrees", 0.0,
        label="Femto Sensor Roll Trim (degrees)",
        minimum=-180.0, maximum=180.0)
    _custom(
        control, calibration_page, "Float",
        "Femtoaudiencenearmetres", 0.25,
        label="Femto Audience Near (metres)",
        minimum=0.10, maximum=15.0)
    _custom(
        control, calibration_page, "Float",
        "Femtoaudiencefarmetres", 12.0,
        label="Femto Audience Far (metres)",
        minimum=0.20, maximum=20.0)
    _custom(
        control, calibration_page, "Pulse",
        "Resetfemtocalibrationtrim", False,
        label="Reset Femto Calibration Trim")

    quality_page = _page(control, "Quality")
    bridge = adapter.op("MOGE2_BRIDGE") if adapter is not None else None
    _custom(
        control, quality_page, "Menu", "Qualityprofile",
        _value(bridge, "Profile", "3080ti_16gb"),
        ("3080ti_16gb", "4090", "5090"), label="GPU Quality Profile")
    reconstruction = pipeline.op("RECONSTRUCTION")
    _custom(
        control, quality_page, "Int", "Geometryresolution",
        int(_value(reconstruction, "Geometryresolution", 384)),
        label="Geometry Resolution")
    _custom(
        control, quality_page, "Toggle", "Preservegeometryaspect",
        bool(_value(reconstruction, "Preservegeometryaspect", True)),
        label="Preserve Source Aspect")
    _custom(
        control, quality_page, "Int", "Pointbudget",
        int(_value(render, "Maxpoints", 120000)), label="Point Budget")
    _custom(
        control, quality_page, "Float", "Pointsize",
        _value(render, "Pointsize", 4.2), label="Point Size")
    _custom(
        control, quality_page, "Int", "Geometryfps",
        int(_value(bridge, "Capturefps", 5)), label="Geometry Capture FPS")
    profile_hint = _custom(
        control, quality_page, "Str", "Profilehint",
        "147k adaptive: 384x384 or 512x288 / 5 Hz",
        label="Profile Guidance")
    try:
        profile_hint.readOnly = True
    except Exception:
        pass

    worker_page = _page(control, "Workers")
    try:
        workspace_root = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".."))
    except Exception:
        workspace_root = ""
    if not os.path.isfile(os.path.join(
            workspace_root, "scripts", "Start-MoGe2Worker.ps1")):
        workspace_root = ""
    _custom(
        control, worker_page, "Str", "Workspaceroot", workspace_root,
        label="Workspace Root")
    _custom(
        control, worker_page, "Int", "Gpuindex", 0,
        label="Physical GPU Index")
    _custom(
        control, worker_page, "Pulse", "Startmogeworker", False,
        label="Start MoGe-2 Worker")
    _custom(
        control, worker_page, "Pulse", "Stopmogeworker", False,
        label="Stop MoGe-2 Worker")
    _custom(
        control, worker_page, "Pulse", "Startdepthanythingworker", False,
        label="Start Depth Anything Worker")
    _custom(
        control, worker_page, "Pulse", "Stopdepthanythingworker", False,
        label="Stop Depth Anything Worker")
    worker_pid = _custom(
        control, worker_page, "Int", "Workerpid", 0,
        label="Last Worker Console PID")
    worker_status = _custom(
        control, worker_page, "Str", "Workerstatus",
        "idle; start one generated-geometry worker at a time",
        label="Worker Status")
    for parameter in (worker_pid, worker_status):
        try:
            parameter.readOnly = True
        except Exception:
            pass

    callbacks = _ensure(
        control, "parameterexecuteDAT", "show_control_callbacks",
        report, optional=True)
    if callbacks is not None:
        try:
            callbacks.text = SHOW_CONTROL_CALLBACKS
        except Exception as exc:
            report.warn("Could not update %s: %s" % (callbacks.path, exc))
        _set(callbacks, "op", control.path)
        _set(callbacks, "pars", "*")
        _set(callbacks, "valuechange", True)
        _set(callbacks, "onpulse", True)
        _set(callbacks, "active", True)
        _style(
            callbacks, -260, 130, (0.36, 0.22, 0.16),
            "Applies only public managed parameters", 250, 100)
    _table(control, "CONTROL_TARGETS", [
        ["control", "managed target"],
        ["Geometry Provider", "STREAMDIFFUSION_ADAPTER/Geometrysource"],
        ["Audio", "optional adapter enable + exclusive voices/soundscape route"],
        ["Display Mode", "WORKING_PIPELINE/Displaymode"],
        ["Completion / Fog", "COMPLETION + view-grade shader constants"],
        ["Color Adjustment", "single, six wall views + stereo grade shaders"],
        ["Interaction", "radius/falloff/timing + per-output enable/intensity"],
        ["VR Simulation", "mock head pose, eye framing and fail-closed headset provider"],
        ["VR Mock Hands", "two sparse hands merged with audience interaction"],
        ["Camera Depth", "selectable webcam Depth Anything or native Femto Mega sensor"],
        ["Camera Calibration", "independent webcam/Femto trims + Femto audience depth gate"],
        ["Panoramic", "wrap camera yaw/FOV + procedural atmosphere"],
        ["Artistic", "side camera yaw/offset + artistic surface FOV"],
        ["Wall Resolution", "single + six feeds; mosaics are 3x wall width"],
        ["Point Cloud Scale", "provider-aware managed camera FOV framing"],
        ["Quality", "geometry grid, point budget/size, bridge capture FPS"],
        ["Workers", "visible PowerShell consoles via public wrapper scripts"],
    ], report)
    _text(
        control, "README_FIRST",
        "SHOW CONTROL\n\n"
        "These controls update only the public FlexGPU managed network. GPU "
        "profiles never modify private StreamDiffusionTD internals or output "
        "resolution. Wall Width and Wall Height control every installation "
        "surface; triple mosaics remain exactly three wall widths. Point "
        "Cloud Scale is creative framing, while the provider scales preserve "
        "separate MoGe-2 and Depth Anything tuning. The Color Adjustment tab "
        "grades the rendered point-cloud views only: single wall, all six "
        "triple-wall feeds, and both stereo preview eyes. Neutral defaults "
        "leave the accepted image unchanged, and Reset Color Adjustment "
        "restores them. VR Simulation is opt-in and defaults to Installation "
        "Only. Desktop Mock moves only the stereo cameras; its two sparse "
        "hand primitives merge with, but never replace, the audience sensor. "
        "Quest/OpenVR remains fail-closed until a physical headset supplies "
        "pose, projection, hand joints and compositor timing. The Camera "
        "Depth tab selects either the existing "
        "webcam + Depth Anything path or the native Femto Mega pointcloud; "
        "only the selected source is enabled. The webcam device name, index, "
        "and mirror values stay intact while Femto is selected. The webcam "
        "worker buttons always switch back to webcam + Depth Anything before "
        "starting that separate no-RGB audience worker. "
        "Use an exact Camera Device Name when Windows virtual cameras occupy "
        "numeric indexes; an empty name keeps the index-based fallback for "
        "other machines. Camera workers start hidden and fail closed when "
        "stopped or stale. Generated-geometry worker buttons open a "
        "visible PowerShell console. Use the matching Stop button before "
        "starting the other provider; Ctrl+C is not required. "
        "The Audio tab mirrors only the optional public adapter contract. "
        "It selects Human Voices Only or Soundscape Only through one exclusive "
        "audiosource_switch and never embeds audio files or private paths. "
        "Panoramic Coverage adds procedural atmosphere only in empty wrap views; "
        "it never stretches the source image. Artistic Surface FOV, Side Yaw, "
        "Side Offset Direction, and Side Offset expose the fixed sculptural "
        "cameras without adding camera animation. Left/Center/Right Wall View "
        "Scale and camera pan affect only the matching panoramic and artistic "
        "wall. Use Apply All after reopening an older saved TOE if you want "
        "every displayed value reapplied.",
        report)
    return control


def install_vr_foundation(root=None):
    """Install only the opt-in desktop VR and mock-hand managed scope.

    The bounded upgrade preserves the accepted installation/triple outputs,
    does not create an active OpenVR/OpenXR operator, and never saves the TOE.
    It is safe to run before a Quest 3 is available.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    reconstruction = pipeline.op("RECONSTRUCTION")
    contract = pipeline.op("RENDER_CONTRACT")
    if reconstruction is None or contract is None:
        raise RuntimeError(
            "RECONSTRUCTION and RENDER_CONTRACT must exist before VR install")

    vr = _build_vr_output(pipeline, report)
    sensor = _build_sensor(pipeline, report)
    point_render = _build_point_render(pipeline, report)
    stereo = _build_stereo(pipeline, report)
    _build_show_control(pipeline, report)

    _connect(reconstruction, sensor, 0, 0, report, replace=True)
    _connect(vr, sensor, 1, 2, report, replace=True)
    _connect(contract, point_render, 0, 0, report, replace=True)
    _connect(contract, point_render, 1, 1, report, replace=True)
    _connect(contract, point_render, 2, 2, report, replace=True)
    _connect(point_render, vr, 0, 1, report, replace=True)
    _connect(point_render, vr, 1, 2, report, replace=True)
    _connect(vr, stereo, 0, 0, report, replace=True)
    _connect(vr, stereo, 1, 1, report, replace=True)
    _align_interaction_position_resolutions(pipeline)
    try:
        pipeline.store("vr_foundation_install_report", report.as_dict())
        pipeline.store("vr_headset_validated", False)
    except Exception:
        pass
    print(
        "[FlexGPU runtime] VR foundation ready disabled: desktop mock head/"
        "hands installed, Quest compositor deferred; TOE remains unsaved")
    return vr


def install_perform_window(root=None):
    """Create the public Perform Mode window for the active installation TOP.

    The bounded upgrade owns only ``INSTALLATION_OUT/window1``.  It does not
    change monitor placement, open a window, enter Perform Mode, inspect a
    private adapter, or save the current TOE.  The exact ``window1`` name
    repairs projects whose Window Placement setting already references the
    canonical FlexGPU path.
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

    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    output = pipeline.op("OUT_DISPLAY_ACTIVE")
    if output is None:
        raise RuntimeError("WORKING_PIPELINE/OUT_DISPLAY_ACTIVE is missing")

    boundary = _ensure(root, "baseCOMP", "INSTALLATION_OUT", report)
    window = _ensure(boundary, "windowCOMP", "window1", report)
    if not _set(window, ("winop", "operator"), output.path):
        raise RuntimeError(
            "%s has no writable Window Operator parameter" % window.path)
    _set(window, "title", "FlexGPU Installation Output")
    _set(window, "interact", False)
    _set(window, "includedialog", True)
    _style(
        window, 180, 20, (0.18, 0.50, 0.28),
        "Perform Mode: WORKING_PIPELINE/OUT_DISPLAY_ACTIVE", 240, 100)
    _text(
        boundary, "README",
        "Projection/LED output boundary. window1 displays "
        "WORKING_PIPELINE/OUT_DISPLAY_ACTIVE and is the canonical Perform "
        "Mode target. Monitor selection and venue placement stay local.",
        report)
    try:
        boundary.store("perform_window_install_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] Perform window ready: %s "
          "(%d created, %d reused, %d warnings)" %
          (window.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return window


def _managed_color_grade_shader_dats(pipeline):
    """Return every public final-view grade DAT and its canonical shader."""

    targets = [(
        "INSTALLATION_OUTPUT/installation_grade_PIXEL",
        "installation_grade",
    )]
    for mode in ("WRAP", "ARTISTIC"):
        for side in ("LEFT", "CENTER", "RIGHT"):
            targets.append((
                "TRIPLE_DISPLAY/GRADE_%s_%s_PIXEL" % (mode, side),
                "installation_grade",
            ))
    for eye in ("LEFT", "RIGHT"):
        targets.append((
            "STEREO_PREVIEW/GRADE_%s_EYE_PIXEL" % eye,
            "view_completion",
        ))
    result = []
    for path, shader_name in targets:
        dat = pipeline.op(path)
        if dat is None:
            raise RuntimeError(
                "managed color-grade shader is missing: %s/%s" %
                (pipeline.path, path))
        result.append((dat, shader_name))
    return result


def _apply_color_grade_shader_values(pipeline, control):
    """Patch current Color Adjustment values into every final-view shader."""

    settings = (
        ("Brightness", "colorBrightness",
         "FLEXGPU_COLOR_BRIGHTNESS", 0.0, -1.0, 1.0),
        ("Contrast", "colorContrast",
         "FLEXGPU_COLOR_CONTRAST", 1.0, 0.0, 3.0),
        ("Saturation", "colorSaturation",
         "FLEXGPU_COLOR_SATURATION", 1.0, 0.0, 3.0),
        ("Gamma", "colorGamma",
         "FLEXGPU_COLOR_GAMMA", 1.0, 0.2, 3.0),
        ("Hueshiftdegrees", "colorHueShiftDegrees",
         "FLEXGPU_COLOR_HUE_SHIFT", 0.0, -180.0, 180.0),
        ("Temperature", "colorTemperature",
         "FLEXGPU_COLOR_TEMPERATURE", 0.0, -1.0, 1.0),
        ("Tint", "colorTint",
         "FLEXGPU_COLOR_TINT", 0.0, -1.0, 1.0),
    )
    grade_dats = [
        dat for dat, _shader_name
        in _managed_color_grade_shader_dats(pipeline)
    ]
    for parameter, symbol, marker, fallback, lower, upper in settings:
        value = max(
            lower, min(upper, float(_value(control, parameter, fallback))))
        _set(control, parameter, value)
        for dat in grade_dats:
            if not _patch_shader_float(dat, symbol, marker, value):
                raise RuntimeError(
                    "%s is missing managed color marker %s" %
                    (dat.path, marker))


def _apply_sensor_calibration_shader_values(pipeline, control):
    """Patch the saved baseline matrix and public neutral trims into GLSL."""

    sensor = pipeline.op("SENSOR_INTERACTION")
    shader = (
        sensor.op("CALIBRATE_SENSOR_POSITION_PIXEL")
        if sensor is not None else None)
    if sensor is None or shader is None:
        raise RuntimeError("managed sensor calibration shader is missing")
    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    for index, fallback in enumerate(identity):
        raw = _value(
            sensor, "Sensortoworld%d" % index,
            " ".join(str(value) for value in fallback))
        try:
            values = [
                float(value)
                for value in str(raw).replace(",", " ").split()]
        except Exception:
            values = list(fallback)
        if (len(values) != 4 or
                not all(math.isfinite(value) for value in values)):
            values = list(fallback)
        if not _patch_shader_vec4(
                shader, "sensorToWorld%d" % index,
                "FLEXGPU_SENSOR_TO_WORLD_%d" % index, values):
            raise RuntimeError(
                "%s is missing sensor-to-world row %d" %
                (shader.path, index))
    source = str(
        _value(control, "Camerasensorsource", "depth_anything")
        ).strip().lower()
    prefix = "Femto" if source == "femto_mega" else "Sensor"
    settings = (
        (prefix + "positionscale", "sensorPositionScale",
         "FLEXGPU_SENSOR_POSITION_SCALE", 1.0, 0.25, 4.0),
        (prefix + "trimxmetres", "sensorTrimXMetres",
         "FLEXGPU_SENSOR_TRIM_X", 0.0, -5.0, 5.0),
        (prefix + "trimymetres", "sensorTrimYMetres",
         "FLEXGPU_SENSOR_TRIM_Y", 0.0, -5.0, 5.0),
        (prefix + "trimzmetres", "sensorTrimZMetres",
         "FLEXGPU_SENSOR_TRIM_Z", 0.0, -5.0, 5.0),
        (prefix + "trimyawdegrees", "sensorTrimYawDegrees",
         "FLEXGPU_SENSOR_TRIM_YAW", 0.0, -180.0, 180.0),
        (prefix + "trimpitchdegrees", "sensorTrimPitchDegrees",
         "FLEXGPU_SENSOR_TRIM_PITCH", 0.0, -90.0, 90.0),
        (prefix + "trimrolldegrees", "sensorTrimRollDegrees",
         "FLEXGPU_SENSOR_TRIM_ROLL", 0.0, -180.0, 180.0),
    )
    for parameter, symbol, marker, fallback, lower, upper in settings:
        value = max(
            lower, min(upper, float(_value(control, parameter, fallback))))
        _set(control, parameter, value)
        if not _patch_shader_float(shader, symbol, marker, value):
            raise RuntimeError(
                "%s is missing managed calibration marker %s" %
                (shader.path, marker))


def _apply_femto_depth_gate_shader_values(pipeline, control):
    """Patch the native sensor's independent mirror and distance gate."""

    femto = pipeline.op("SOURCES/FEMTO_MEGA_ADAPTER")
    validity_shader = (
        femto.op("DERIVE_SENSOR_VALIDITY_PIXEL")
        if femto is not None else None)
    position_shader = (
        femto.op("CONVERT_SENSOR_POSITION_PIXEL")
        if femto is not None else None)
    if femto is None or validity_shader is None or position_shader is None:
        raise RuntimeError("managed Femto position/gate shader is missing")
    mirrored = bool(_value(control, "Femtomirrorhorizontal", False))
    near_metres = max(
        0.10, min(
            15.0,
            float(_value(control, "Femtoaudiencenearmetres", 0.25))))
    far_metres = max(
        near_metres + 0.10,
        min(
            20.0,
            float(_value(control, "Femtoaudiencefarmetres", 12.0))))
    _set(control, "Femtomirrorhorizontal", mirrored)
    _set(control, "Femtoaudiencenearmetres", near_metres)
    _set(control, "Femtoaudiencefarmetres", far_metres)
    if not _patch_shader_float(
            position_shader, "femtoMirrorHorizontal",
            "FLEXGPU_FEMTO_MIRROR_HORIZONTAL",
            1.0 if mirrored else 0.0):
        raise RuntimeError(
            "%s is missing managed Femto mirror marker" %
            position_shader.path)
    for symbol, marker, value in (
            ("femtoNearMetres", "FLEXGPU_FEMTO_NEAR_METRES", near_metres),
            ("femtoFarMetres", "FLEXGPU_FEMTO_FAR_METRES", far_metres)):
        if not _patch_shader_float(
                validity_shader, symbol, marker, value):
            raise RuntimeError(
                "%s is missing managed Femto gate marker %s" %
                (validity_shader.path, marker))


def install_camera_calibration_controls(root=None):
    """Add non-destructive audience-camera calibration trims to Show Control.

    The four local ``Sensortoworld`` rows remain the authoritative venue
    baseline. This bounded upgrade replaces only the managed calibration,
    Femto conversion/validity, and interaction-field shader sources, repairs
    the managed live-position-to-sensor wire, and updates ``SHOW_CONTROL``.
    Webcam and Femto trims are independent; neither reset pulse edits the saved
    baseline. The installer does not inspect private adapters, start a worker,
    or save the TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    sensor = pipeline.op("SENSOR_INTERACTION")
    shader = (
        sensor.op("CALIBRATE_SENSOR_POSITION_PIXEL")
        if sensor is not None else None)
    if sensor is None or shader is None:
        raise RuntimeError("managed sensor calibration stage is missing")
    interaction_shader = sensor.op("interaction_field_PIXEL")
    if interaction_shader is None:
        raise RuntimeError("managed interaction field shader is missing")

    control = _build_show_control(pipeline, report)
    shader.text = SHADERS["sensor_to_world"]
    interaction_shader.text = SHADERS["interaction_field"]
    femto = pipeline.op("SOURCES/FEMTO_MEGA_ADAPTER")
    femto_position = (
        femto.op("CONVERT_SENSOR_POSITION_PIXEL")
        if femto is not None else None)
    femto_validity = (
        femto.op("DERIVE_SENSOR_VALIDITY_PIXEL")
        if femto is not None else None)
    if femto_position is None or femto_validity is None:
        raise RuntimeError("managed Femto position/validity shader is missing")
    femto_position.text = SHADERS["femto_sensor_position"]
    femto_validity.text = SHADERS["femto_sensor_validity"]
    world_position = _interaction_world_position_source(pipeline)
    if world_position is None:
        raise RuntimeError("managed live position contract is missing")
    _connect(world_position, sensor, 0, 0, report, replace=True)
    _apply_sensor_calibration_shader_values(pipeline, control)
    _apply_femto_depth_gate_shader_values(pipeline, control)
    try:
        control.store(
            "camera_calibration_controls_report", report.as_dict())
    except Exception:
        pass
    print(
        "[FlexGPU runtime] Camera Calibration trims ready: independent "
        "webcam/Femto profiles, Femto audience gate, managed world input "
        "connected, saved sensor-to-world baseline preserved; "
        "TOE remains unsaved")
    return control


def install_color_adjustment_controls(root=None):
    """Add a neutral final-view Color Adjustment tab to an existing TOE.

    This bounded upgrade replaces only the nine public final-view grade shader
    DAT sources and updates ``SHOW_CONTROL``. It does not touch source RGB,
    geometry, cameras, resolution, private adapters, or save the current TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")

    control = _build_show_control(pipeline, report)
    grade_dats = _managed_color_grade_shader_dats(pipeline)
    for dat, shader_name in grade_dats:
        try:
            dat.text = SHADERS[shader_name]
        except Exception as exc:
            raise RuntimeError(
                "could not update %s: %s" % (dat.path, exc))

    density = max(
        0.0, min(1.5, float(_value(control, "Fogdensity", 0.35))))
    for dat, _shader_name in grade_dats:
        if not _patch_shader_float(
                dat, "viewFogDensity",
                "FLEXGPU_VIEW_FOG_DENSITY", density):
            raise RuntimeError(
                "%s is missing managed fog marker" % dat.path)
    _apply_color_grade_shader_values(pipeline, control)
    try:
        control.store("color_adjustment_controls_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] Color Adjustment tab ready for single, triple "
          "and stereo point-cloud views; neutral defaults preserve the "
          "accepted visual; TOE remains unsaved")
    return control


def install_output_framing_controls(root=None):
    """Add adjustable wall resolution, provider framing and worker buttons.

    This bounded upgrade touches only managed Camera/Render parameters and the
    public ``SHOW_CONTROL`` component. It does not rebuild the point world,
    inspect private adapters, start a worker automatically, or save the TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    render = pipeline.op("POINT_RENDER")
    if render is None:
        raise RuntimeError("POINT_RENDER is missing")

    _apply_point_cloud_camera_framing(render)
    control = _build_show_control(pipeline, report)
    try:
        control.store("output_framing_controls_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] adjustable output/framing controls ready: "
          "wall width/height, provider-aware point-cloud scale and visible "
          "worker consoles; TOE remains unsaved")
    return control


def install_worker_stop_controls(root=None):
    """Add checkout-scoped worker stop buttons to an existing local TOE.

    This bounded upgrade updates only the public ``SHOW_CONTROL`` component.
    Stop actions call the public provider-specific wrapper for this exact
    checkout; the installer itself does not start or stop a worker, inspect
    private adapters, rebuild the network, or save the TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")

    control = _build_show_control(pipeline, report)
    try:
        control.store("worker_stop_controls_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] checkout-scoped MoGe-2 and Depth Anything "
          "worker stop buttons ready; TOE remains unsaved")
    return control


def install_camera_depth_controls(root=None):
    """Add bounded audience-camera controls to an existing local TOE.

    This upgrade refreshes only the public ``SHOW_CONTROL`` component. It
    neither opens a camera nor starts a worker, and it does not change private
    components, generated-image geometry routing, or save the current TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    sensor = pipeline.op("SENSOR_INTERACTION")
    adapter = (
        sensor.op("DEPTH_SENSOR_ADAPTER") if sensor is not None else None)
    bridge = (
        adapter.op("DEPTH_ANYTHING_BRIDGE")
        if adapter is not None else None)
    if sensor is None or adapter is None or bridge is None:
        raise RuntimeError(
            "camera Depth Anything sensor bridge is not installed")

    control = _build_show_control(pipeline, report)
    try:
        control.store("camera_depth_controls_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] camera Depth Anything controls ready; "
          "camera remains closed and TOE remains unsaved")
    return control


def install_interaction_routing_controls(root=None):
    """Add per-output interaction routing to an existing local TOE.

    The bounded upgrade keeps the persistent/completed world
    interaction-neutral, creates four managed render-position branches, and
    refreshes only ``POINT_RENDER`` plus the public ``SHOW_CONTROL``. Existing
    private adapters, source routing, wall calibration and user-authored
    operators remain untouched. The installer does not start a worker or save
    the current TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    contract = pipeline.op("RENDER_CONTRACT")
    if contract is None:
        raise RuntimeError("RENDER_CONTRACT is missing")
    sensor = pipeline.op("SENSOR_INTERACTION")
    if sensor is None:
        raise RuntimeError("SENSOR_INTERACTION is missing")
    sensor_page = _page(sensor, "Sensor")
    _custom(
        sensor, sensor_page, "Float", "Interactionradius", 0.55,
        label="Interaction Radius (metres)",
        minimum=0.05, maximum=3.0)
    _custom(
        sensor, sensor_page, "Float", "Interactionfalloff", 1.0,
        label="Interaction Falloff",
        minimum=0.25, maximum=4.0)
    _custom(
        sensor, sensor_page, "Float", "Interactionsmoothing", 0.35,
        label="Interaction Smoothing",
        minimum=0.0, maximum=0.92)
    _custom(
        sensor, sensor_page, "Float", "Interactionresponse", 0.65,
        label="Interaction Response",
        minimum=0.0, maximum=1.0)
    _custom(
        sensor, sensor_page, "Float", "Interactiondecay", 0.5,
        label="Interaction Decay",
        minimum=0.0, maximum=1.0)
    interaction_pixel = sensor.op("interaction_field_PIXEL")
    smoothing_pixel = sensor.op("INTERACTION_SMOOTH_PIXEL")
    if interaction_pixel is None or smoothing_pixel is None:
        raise RuntimeError("managed interaction shaders are missing")
    interaction_pixel.text = SHADERS["interaction_field"]
    smoothing_pixel.text = SHADERS["interaction_smoothing"]
    for path, shader_name in (
            ("TEMPORAL_WORLD/ADVECT_HISTORY_PIXEL", "temporal_advect"),
            ("COMPLETION/procedural_backfill_PIXEL", "procedural_backfill")):
        dat = pipeline.op(path)
        if dat is None:
            raise RuntimeError("managed shader is missing: " + path)
        dat.text = SHADERS[shader_name]

    render = _build_point_render(pipeline, report)
    _connect(contract, render, 2, 2, report, replace=True)
    control = _build_show_control(pipeline, report)
    try:
        control.store(
            "interaction_routing_controls_report", report.as_dict())
    except Exception:
        pass
    print(
        "[FlexGPU runtime] interaction routing ready: installation and center "
        "enabled; left and right disabled; independent intensities available; "
        "TOE remains unsaved")
    return control


def install_wall_view_controls(root=None):
    """Add wall scales and artistic offset direction to an existing TOE.

    This bounded upgrade changes only the six managed triple-wall camera
    FOV/pan expressions, the two artistic side-camera X expressions, related
    render parameters, and the public ``SHOW_CONTROL`` component. It does not
    touch single/stereo cameras, geometry, resolution, or private adapters.
    The current TOE is not saved.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    render = pipeline.op("POINT_RENDER")
    if render is None:
        raise RuntimeError("POINT_RENDER is missing")
    left = render.op("CAMERA_ARTISTIC_LEFT")
    right = render.op("CAMERA_ARTISTIC_RIGHT")
    if left is None or right is None:
        raise RuntimeError("managed artistic side cameras are missing")

    render_page = _page(render, "Render")
    _custom(
        render, render_page, "Menu", "Artisticoffsetdirection", "outward",
        menu=("outward", "inward"),
        label="Artistic Side Offset Direction")
    for side in ("Left", "Center", "Right"):
        _custom(
            render, render_page, "Float", side + "wallscale", 1.0,
            label=side + " Wall View Scale")
        _custom(
            render, render_page, "Float",
            side + "wallpanhorizontaldegrees", 0.0,
            label=side + " Camera Horizontal Pan (degrees)")
        _custom(
            render, render_page, "Float",
            side + "wallpanverticaldegrees", 0.0,
            label=side + " Camera Vertical Pan (degrees)")
    direction = str(_value(
        render, "Artisticoffsetdirection", "outward")).strip().lower()
    if direction not in ("outward", "inward"):
        direction = "outward"
        _set(render, "Artisticoffsetdirection", direction)
    offset_direction = (
        "(1.0 if parent().par.Artisticoffsetdirection.eval() == "
        "'outward' else -1.0)")
    _expr(
        left, "tx",
        "parent().par.Artisticoffsetmetres.eval() * " + offset_direction)
    _expr(
        right, "tx",
        "-parent().par.Artisticoffsetmetres.eval() * " + offset_direction)
    wrap_yaw = {
        "LEFT": "parent().par.Wrapyawdegrees.eval()",
        "CENTER": "0.0",
        "RIGHT": "-parent().par.Wrapyawdegrees.eval()",
    }
    artistic_yaw = {
        "LEFT": "-parent().par.Artisticyawdegrees.eval()",
        "CENTER": "0.0",
        "RIGHT": "parent().par.Artisticyawdegrees.eval()",
    }
    for side in ("LEFT", "CENTER", "RIGHT"):
        wall_scale_expression = (
            "parent().par.%swallscale.eval()" % side.title())
        pan_horizontal = (
            "parent().par.%swallpanhorizontaldegrees.eval()" % side.title())
        pan_vertical = (
            "parent().par.%swallpanverticaldegrees.eval()" % side.title())
        for mode, base_yaw, base_fov in (
                ("WRAP", wrap_yaw[side],
                 "parent().par.Wrapfovdegrees.eval()"),
                ("ARTISTIC", artistic_yaw[side],
                 "parent().par.Surfacefovdegrees.eval()")):
            camera = render.op("CAMERA_" + mode + "_" + side)
            _expr(camera, "rx", pan_vertical)
            _expr(
                camera, "ry",
                "(%s) + (%s)" % (base_yaw, pan_horizontal))
            _expr(
                camera, "fov",
                _scaled_camera_fov_expression(
                    base_fov, wall_scale_expression))
    control = _build_show_control(pipeline, report)
    _set(control, "Artisticoffsetdirection", direction)
    try:
        render.store(
            "artistic_wall_offset_direction",
            "show-control selectable: outward or inward screen motion")
        control.store(
            "wall_view_controls_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] wall view controls ready: left/center/right "
          "scales and camera pan plus outward/inward artistic offset; "
          "TOE remains unsaved")
    return control


def install_show_control_upgrade(root=None):
    """Install panoramic coverage, interaction smoothing and show controls.

    The bounded upgrade changes only public operators below WORKING_PIPELINE.
    Single and artistic rendering inputs remain untouched, no node is removed,
    and the current TOE is not saved.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")

    render = pipeline.op("POINT_RENDER")
    if render is None:
        raise RuntimeError("POINT_RENDER is missing")
    render_page = _page(render, "Render")
    _custom(render, render_page, "Float", "Wrapfovdegrees", 78.0,
            label="Panoramic Surface Camera FOV (degrees)")
    for side in ("LEFT", "CENTER", "RIGHT"):
        camera = render.op("CAMERA_WRAP_" + side)
        if camera is not None:
            _expr(camera, "fov", "parent().par.Wrapfovdegrees.eval()")
    _apply_point_cloud_camera_framing(render)

    sensor = pipeline.op("SENSOR_INTERACTION")
    if sensor is None:
        raise RuntimeError("SENSOR_INTERACTION is missing")
    sensor_page = _page(sensor, "Sensor")
    _custom(sensor, sensor_page, "Float", "Interactionsmoothing", 0.35,
            label="Interaction Smoothing")
    raw_interaction = sensor.op("interaction_field")
    if raw_interaction is None:
        raise RuntimeError("managed interaction field is missing")
    interaction = _build_interaction_smoothing(
        sensor, raw_interaction, report)
    _out_top(sensor, "OUT_INTERACTION", interaction, 1, report)
    debug = sensor.op("INTERACTION_DEBUG")
    if debug is not None:
        _connect(interaction, debug, 0, 0, report, replace=True)

    # Rebuild only the public triple-display stage. Preserve the commissioned
    # per-wall resolution while its six inputs and output connector order stay
    # stable; artistic grade inputs remain direct.
    existing_triple = pipeline.op("TRIPLE_DISPLAY")
    reference_grade = (
        existing_triple.op("GRADE_WRAP_CENTER")
        if existing_triple is not None else None)
    surface_width = int(_value(reference_grade, ("resolutionw", "resw"), 1920))
    surface_height = int(_value(reference_grade, ("resolutionh", "resh"), 1080))
    triple = _build_triple_display(pipeline, report)
    for mode in ("WRAP", "ARTISTIC"):
        for side in ("LEFT", "CENTER", "RIGHT"):
            if mode == "WRAP":
                _set_resolution(
                    triple.op("COVERAGE_WRAP_" + side),
                    surface_width, surface_height)
            _set_resolution(
                triple.op("GRADE_%s_%s" % (mode, side)),
                surface_width, surface_height)
        _set_resolution(
            triple.op(mode + "_MOSAIC"),
            surface_width * 3, surface_height)
        _set_resolution(
            triple.op(mode + "_MOSAIC_FALLBACK"),
            surface_width * 3, surface_height)
    control = _build_show_control(pipeline, report)
    try:
        control.store("show_control_upgrade_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] show-control upgrade ready: %s "
          "(%d created, %d reused, %d warnings)" %
          (control.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return control


def install_audio_source_controls(root=None):
    """Add optional public audio controls without importing local media.

    The bounded upgrade adds only the hardware-neutral adapter parameters and
    refreshes ``SHOW_CONTROL`` plus its callback DAT. It never creates audio
    tracks, embeds machine paths, inspects private components, or saves the TOE.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")
    adapter = pipeline.op("SOURCES/STREAMDIFFUSION_ADAPTER")
    if adapter is None:
        raise RuntimeError("STREAMDIFFUSION_ADAPTER is missing")

    _ensure_audio_adapter_contract(adapter)
    control = _build_show_control(pipeline, report)
    try:
        control.store("audio_source_controls_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU runtime] optional audio-source controls ready: %s "
          "(%d created, %d reused, %d warnings); TOE remains unsaved" %
          (control.path, len(report.created), len(report.reused),
           len(report.warnings)))
    return control


def install_adaptive_source_resolution(root=None):
    """Preserve generated-image aspect within the 3080 geometry pixel budget.

    This bounded upgrade refreshes only ``RECONSTRUCTION`` and the public
    ``SHOW_CONTROL``. A geometry resolution of 384 remains 147,456 pixels, so
    square input becomes 384x384 and 16:9 input becomes 512x288. Projector
    render TOPs, private adapters, model settings, and the current TOE save are
    untouched.
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
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")

    reconstruction = _build_reconstruction(pipeline, report)
    control = _build_show_control(pipeline, report)
    try:
        reconstruction.store(
            "adaptive_source_resolution_install_report", report.as_dict())
        reconstruction.store("geometry_pixel_budget", 384 * 384)
        reconstruction.store(
            "adaptive_geometry_examples",
            {"512x512": "384x384",
             "1024x567": "512x284",
             "1024x576": "512x288"})
    except Exception:
        pass
    print("[FlexGPU runtime] adaptive source resolution ready: "
          "512x512 -> 384x384; 1024x567 -> 512x284; "
          "1024x576 -> 512x288; "
          "wall feeds unchanged (%d created, %d reused, %d warnings)" %
          (len(report.created), len(report.reused), len(report.warnings)))
    return reconstruction


def install_noncommercial_preview_outputs(root=None):
    """Fit every development preview inside the 1280x1280 NC limit.

    Individual single/triple wall feeds become 1280x720. The two horizontal
    triple mosaics become 1280x240, preserving their commissioned 16:3 aspect
    without creating an over-limit 3840-pixel TOP. Stereo preview becomes two
    640x360 eyes in one 1280x360 TOP. The production 1920x1080 contract can be
    restored later with ``install_venue_1080p_outputs``.
    """

    if root is None:
        root = _op(ROOT_PATH)
    elif isinstance(root, str):
        root = _op(root)
    if root is None:
        raise RuntimeError("FlexGPU root %s does not exist" % ROOT_PATH)
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")

    wall_width, wall_height = 1280, 720
    for path in (
        "POINT_RENDER/METRIC_RENDER_CENTER",
        "POINT_RENDER/METRIC_MONO_FALLBACK",
        "INSTALLATION_OUTPUT/installation_grade",
    ):
        _set_resolution(pipeline.op(path), wall_width, wall_height)
    for mode in ("WRAP", "ARTISTIC"):
        for side in ("LEFT", "CENTER", "RIGHT"):
            _set_resolution(
                pipeline.op("POINT_RENDER/METRIC_RENDER_%s_%s" % (mode, side)),
                wall_width, wall_height)
            if mode == "WRAP":
                _set_resolution(
                    pipeline.op("TRIPLE_DISPLAY/COVERAGE_WRAP_" + side),
                    wall_width, wall_height)
            _set_resolution(
                pipeline.op("TRIPLE_DISPLAY/GRADE_%s_%s" % (mode, side)),
                wall_width, wall_height)
        _set_resolution(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC" % mode), 1280, 240)
        _set_horizontal_layout(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC" % mode))
        _set_resolution(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC_FALLBACK" % mode),
            1280, 240)
        _set_horizontal_layout(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC_FALLBACK" % mode))
    for eye in ("LEFT", "RIGHT"):
        _set_resolution(
            pipeline.op("STEREO_PREVIEW/GRADE_%s_EYE" % eye), 640, 360)
    _set_resolution(
        pipeline.op("STEREO_PREVIEW/STEREO_SIDE_BY_SIDE"), 1280, 360)
    _set_horizontal_layout(
        pipeline.op("STEREO_PREVIEW/STEREO_SIDE_BY_SIDE"))
    _set_resolution(
        pipeline.op("STEREO_PREVIEW/STEREO_SIDE_BY_SIDE_FALLBACK"),
        1280, 360)
    _set_horizontal_layout(
        pipeline.op("STEREO_PREVIEW/STEREO_SIDE_BY_SIDE_FALLBACK"))
    try:
        pipeline.store("venue_output_profile", "noncommercial_preview")
        pipeline.store(
            "production_output_contract",
            "single/six walls 1920x1080; mosaics 5760x1080")
    except Exception:
        pass
    control = pipeline.op("SHOW_CONTROL")
    _set(control, "Wallwidth", wall_width)
    _set(control, "Wallheight", wall_height)
    print("[FlexGPU runtime] non-commercial preview ready: "
          "single/six wall feeds 1280x720; mosaics 1280x240; "
          "production 1080p contract retained for later restore; TOE unsaved")
    return pipeline


def install_venue_1080p_outputs(root=None):
    """Set the single wall and every triple-wall feed to native 1920x1080.

    This bounded upgrade changes only public managed TOP resolutions. It does
    not rebuild the working pipeline, change GPU/geometry budgets, touch
    private adapters, alter stereo preview resolution, or save the TOE.
    """

    if root is None:
        root = _op(ROOT_PATH)
    elif isinstance(root, str):
        root = _op(root)
    if root is None:
        raise RuntimeError("FlexGPU root %s does not exist" % ROOT_PATH)
    pipeline = root.op(PIPELINE_NAME)
    if pipeline is None:
        raise RuntimeError("WORKING_PIPELINE is missing; build it first")

    wall_width, wall_height = 1920, 1080
    for path in (
        "POINT_RENDER/METRIC_RENDER_CENTER",
        "POINT_RENDER/METRIC_MONO_FALLBACK",
        "INSTALLATION_OUTPUT/installation_grade",
    ):
        _set_resolution(pipeline.op(path), wall_width, wall_height)
    for mode in ("WRAP", "ARTISTIC"):
        for side in ("LEFT", "CENTER", "RIGHT"):
            _set_resolution(
                pipeline.op("POINT_RENDER/METRIC_RENDER_%s_%s" % (mode, side)),
                wall_width, wall_height)
            if mode == "WRAP":
                _set_resolution(
                    pipeline.op("TRIPLE_DISPLAY/COVERAGE_WRAP_" + side),
                    wall_width, wall_height)
            _set_resolution(
                pipeline.op("TRIPLE_DISPLAY/GRADE_%s_%s" % (mode, side)),
                wall_width, wall_height)
        _set_resolution(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC" % mode),
            wall_width * 3, wall_height)
        _set_horizontal_layout(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC" % mode))
        _set_resolution(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC_FALLBACK" % mode),
            wall_width * 3, wall_height)
        _set_horizontal_layout(
            pipeline.op("TRIPLE_DISPLAY/%s_MOSAIC_FALLBACK" % mode))
    # Keep the desktop stereo preview at its existing development resolution,
    # but repair older TOEs whose Layout TOP silently retained Align=None.
    _set_horizontal_layout(
        pipeline.op("STEREO_PREVIEW/STEREO_SIDE_BY_SIDE"))
    _set_horizontal_layout(
        pipeline.op("STEREO_PREVIEW/STEREO_SIDE_BY_SIDE_FALLBACK"))
    try:
        pipeline.store("venue_output_profile", "venue_1080p")
    except Exception:
        pass
    control = pipeline.op("SHOW_CONTROL")
    _set(control, "Wallwidth", wall_width)
    _set(control, "Wallheight", wall_height)
    print("[FlexGPU runtime] venue outputs ready: single and six wall feeds "
          "1920x1080; mosaics 5760x1080; TOE remains unsaved")
    return pipeline


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
    _custom(
        pipeline, page, "Menu", "Displaymode", "single",
        menu=("single", "panoramic_wrap", "artistic_multi_angle"),
        label="Active Installation Display")

    sources = _build_sources(pipeline, report)
    role_bridge = _build_role_bridge(pipeline, report)
    reconstruction = _build_reconstruction(pipeline, report)
    vr = _build_vr_output(pipeline, report)
    sensor = _build_sensor(pipeline, report)
    temporal = _build_persistence(pipeline, report)
    completion = _build_completion(pipeline, report)
    contract = _build_render_contract(pipeline, report)
    point_render = _build_point_render(pipeline, report)
    installation = _build_installation(pipeline, report)
    triple = _build_triple_display(pipeline, report)
    stereo = _build_stereo(pipeline, report)
    _build_show_control(pipeline, report)

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
    _connect(vr, sensor, 1, 2, report, replace=True)
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
    # POINT_RENDER receives POSITION, COLOR and INTERACTION. The renderer keeps
    # an interaction-neutral base world and applies the field independently to
    # installation, left, center and right view branches.
    _connect(contract, point_render, 0, 0, report, replace=True)
    _connect(contract, point_render, 1, 1, report, replace=True)
    _connect(contract, point_render, 2, 2, report, replace=True)
    _align_interaction_position_resolutions(pipeline)
    _connect(point_render, installation, 0, 0, report, replace=True)
    _connect(completion, installation, 1, 1, report, replace=True)
    for destination_index, source_index in enumerate(range(3, 9)):
        _connect(point_render, triple, destination_index, source_index, report, replace=True)
    _connect(completion, triple, 6, 1, report, replace=True)
    _connect(point_render, vr, 0, 1, report, replace=True)
    _connect(point_render, vr, 1, 2, report, replace=True)
    _connect(vr, stereo, 0, 0, report, replace=True)
    _connect(vr, stereo, 1, 1, report, replace=True)

    display_route = _ensure(
        pipeline, "switchTOP", "DISPLAY_MODE_ROUTE", report)
    _connect(installation, display_route, 0, 0, report, replace=True)
    _connect(triple, display_route, 1, 3, report, replace=True)
    _connect(triple, display_route, 2, 7, report, replace=True)
    _expr(display_route, "index", "parent().par.Displaymode.menuIndex")

    # Easy-to-find root outputs for projectors, recorders, transports and later
    # VR runtimes.  They mirror the internal stable contract names.
    outputs = (
        ("OUT_POSITION", contract, 0),
        # Preserve the geometry-aligned OUT_COLOR contract used by validators
        # and downstream point renderers. Expose the exact synchronized source
        # separately so it can be compared without fog/procedural completion.
        ("OUT_SOURCE_COLOR", role_bridge, 0),
        ("OUT_COLOR", contract, 1),
        ("OUT_INTERACTION", contract, 2),
        ("OUT_INSTALLATION", installation, 0),
        ("OUT_TRIPLE_WRAP", triple, 3),
        ("OUT_TRIPLE_ARTISTIC", triple, 7),
        ("OUT_DISPLAY_ACTIVE", display_route, 0),
        ("OUT_TRIPLE_WRAP_LEFT", triple, 0),
        ("OUT_TRIPLE_WRAP_CENTER", triple, 1),
        ("OUT_TRIPLE_WRAP_RIGHT", triple, 2),
        ("OUT_TRIPLE_ARTISTIC_LEFT", triple, 4),
        ("OUT_TRIPLE_ARTISTIC_CENTER", triple, 5),
        ("OUT_TRIPLE_ARTISTIC_RIGHT", triple, 6),
        ("OUT_LEFT_EYE", stereo, 0),
        ("OUT_RIGHT_EYE", stereo, 1),
        ("OUT_STEREO_PREVIEW", stereo, 2),
        ("OUT_TEMPORAL_STATE", contract, 3),
        ("OUT_SENSOR_POSITION", sensor, 0),
        ("OUT_INTERACTION_DEBUG", sensor, 3),
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
        ["triple_wrap_output",
         "OUT_TRIPLE_WRAP_LEFT/CENTER/RIGHT + OUT_TRIPLE_WRAP"],
        ["triple_artistic_output",
         "OUT_TRIPLE_ARTISTIC_LEFT/CENTER/RIGHT + OUT_TRIPLE_ARTISTIC"],
        ["active_display_output", "OUT_DISPLAY_ACTIVE"],
        ["show_control", "SHOW_CONTROL (public live parameters only)"],
        ["vr_foundation", "VR_OUTPUT: mock head/hands plus deferred headset contract"],
        ["stereo_output", "OUT_LEFT_EYE, OUT_RIGHT_EYE, OUT_STEREO_PREVIEW"],
        ["interaction_debug_output", "OUT_INTERACTION_DEBUG (visualization only)"],
        ["openvr_dependency", "deferred; no headset operator is active without hardware"],
        ["unknown_nodes", "preserved"],
    ], report)
    _text(pipeline, "README_FIRST", "FLEXGPU WORKING PIPELINE\n\n"
          "Open OUT_INSTALLATION for the unchanged single point-cloud render, "
          "OUT_TRIPLE_WRAP or OUT_TRIPLE_ARTISTIC for three-surface mosaics, "
          "and OUT_DISPLAY_ACTIVE for the mode selected on WORKING_PIPELINE. "
          "Each triple mode also exposes independent LEFT/CENTER/RIGHT TOPs. "
          "Open OUT_STEREO_PREVIEW for a desktop stereo view and VR_OUTPUT "
          "for the opt-in mock head/hand adapter. "
          "Open SHOW_CONTROL for geometry provider, display, completion, "
          "interaction, panoramic coverage and GPU quality presets. "
          "The animated RGB/depth and sensor sources work immediately. "
          "Later, replace only the two "
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
