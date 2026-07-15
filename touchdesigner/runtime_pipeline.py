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


BUILD_VERSION = "1.1.0"
ROOT_PATH = "/project1/flexgpu"
PIPELINE_NAME = "WORKING_PIPELINE"


# These names are deliberately public and covered by source tests.  They form
# the stable integration surface shared by the demo, StreamDiffusionTD, the
# point renderer, installation output, and a later headset-specific renderer.
TOP_CONTRACTS = {
    "RGB": "RGBA color TOP; linear or sRGB, alpha=1",
    "DEPTH": "R depth TOP normalized 0..1; near is 0, far is 1",
    "POSITION": "RGBA32F TOP; RGB=XYZ metres, A=active/valid",
    "COLOR": "RGBA16F or RGBA8 TOP aligned pixel-for-pixel with POSITION",
    "SENSOR_POSITION": "RGBA32F TOP; RGB=XYZ metres, A=occupancy",
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
    "depth_to_position": r'''// CONTRACT: RGB + DEPTH -> POSITION (XYZ metres + active alpha)
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    float depth01 = clamp(texture(sTD2DInputs[1], uv).r, 0.0, 1.0);
    float valid = step(0.002, depth01) * (1.0 - step(0.998, depth01));
    const float nearMetres = 0.35;
    const float farMetres = 4.50;
    const float tanHalfFovY = 0.57735026919; // 60 degree vertical FOV
    float z = mix(nearMetres, farMetres, depth01);
    vec2 ndc = uv * 2.0 - 1.0;
    float aspect = float(textureSize(sTD2DInputs[1], 0).x) /
                   max(1.0, float(textureSize(sTD2DInputs[1], 0).y));
    vec3 position = vec3(ndc.x * aspect * z * tanHalfFovY,
                         -ndc.y * z * tanHalfFovY,
                         -z);
    fragColor = TDOutputSwizzle(vec4(position, valid));
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
    "interaction_field": r'''// CONTRACT: POSITION + SENSOR MASK -> INTERACTION force + occupancy
out vec4 fragColor;

float hash21(vec2 p)
{
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

void main()
{
    vec2 uv = vUV.st;
    vec4 point = texture(sTD2DInputs[0], uv);
    float occupancy = texture(sTD2DInputs[1], uv).r;
    vec2 radial = uv - vec2(0.5);
    vec3 force = vec3(radial * (1.8 + hash21(uv) * 0.4), 0.35) * occupancy;
    force *= point.a;
    fragColor = TDOutputSwizzle(vec4(force, occupancy));
}
''',
    "temporal_persistence": r'''// CONTRACT: POSITION + HISTORY + INTERACTION -> PERSISTENT_POSITION
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 current = texture(sTD2DInputs[0], uv);
    vec4 history = texture(sTD2DInputs[1], uv);
    vec4 interaction = texture(sTD2DInputs[2], uv);
    const float persistenceDecay = 0.985;
    const float newFrameBlend = 0.36;
    float hasCurrent = step(0.5, current.a);
    float hasHistory = step(0.001, history.a);
    vec3 carried = history.rgb + interaction.rgb * 0.006 * history.a;
    // Seed immediately on the first valid frame; low-pass only once history exists.
    float blend = hasCurrent * mix(1.0, newFrameBlend, hasHistory);
    vec3 position = mix(carried, current.rgb, blend);
    float activity = max(current.a, history.a * persistenceDecay);
    fragColor = TDOutputSwizzle(vec4(position, activity));
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
    vec2 uv = vUV.st;
    vec4 position = texture(sTD2DInputs[0], uv);
    vec4 source = texture(sTD2DInputs[1], uv);
    vec2 texel = 1.0 / vec2(textureSize(sTD2DInputs[0], 0));
    float nearby = max(max(texture(sTD2DInputs[0], uv + vec2(texel.x, 0.0)).a,
                           texture(sTD2DInputs[0], uv - vec2(texel.x, 0.0)).a),
                       max(texture(sTD2DInputs[0], uv + vec2(0.0, texel.y)).a,
                           texture(sTD2DInputs[0], uv - vec2(0.0, texel.y)).a));
    float disocclusion = nearby * (1.0 - position.a);
    float noiseFog = smoothstep(0.30, 0.90, hash21(floor(uv * 420.0)));
    float fogBase = disocclusion * (0.45 + noiseFog * 0.50) +
                    (1.0 - position.a) * noiseFog * 0.12;
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
    "installation_grade": r'''// CONTRACT: POINT_RENDER + FOG_PLATE -> INSTALLATION
out vec4 fragColor;

void main()
{
    vec2 uv = vUV.st;
    vec4 points = texture(sTD2DInputs[0], uv);
    vec4 fog = texture(sTD2DInputs[1], uv);
    vec2 p = uv * 2.0 - 1.0;
    float vignette = smoothstep(1.35, 0.24, dot(p, p));
    vec3 color = points.rgb + fog.rgb * fog.a * 0.32;
    color = color / (1.0 + color); // inexpensive tone map
    color *= mix(0.54, 1.0, vignette);
    fragColor = TDOutputSwizzle(vec4(color, 1.0));
}
''',
    "transport_pack_atlas": r'''// CONTRACT: RGB + DEPTH -> atomic RGBA16F ATLAS (left RGB, right depth)
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
        float depth = clamp(texture(sTD2DInputs[1], sourceUV).r, 0.0, 1.0);
        fragColor = TDOutputSwizzle(vec4(depth, depth, depth, 1.0));
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
    "transport_unpack_depth": r'''// CONTRACT: atomic ATLAS -> normalized DEPTH (right half)
out vec4 fragColor;

void main()
{
    vec2 sourceUV = vec2(0.5 + vUV.st.x * 0.5, vUV.st.y);
    float depth = clamp(texture(sTD2DInputs[0], sourceUV).r, 0.0, 1.0);
    fragColor = TDOutputSwizzle(vec4(depth, depth, depth, 1.0));
}
''',
}


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


def _ensure(parent, type_name, name, report, optional=False):
    found = _child(parent, name)
    if found is not None:
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
    _connect(source, node, report=report)
    return node


def _glsl(parent, name, shader_name, inputs, report, float_output=False):
    source = _text(parent, "%s_PIXEL" % name, SHADERS[shader_name], report)
    node = _ensure(parent, "glslTOP", name, report)
    _set(node, ("pixeldat", "pixelshader"), source.path)
    _set(node, "outputresolution", "useinput")
    if float_output:
        _set(node, "format", "rgba32float")
    else:
        _set(node, "format", "rgba16float")
    for index, input_node in enumerate(inputs):
        _connect(input_node, node, index, 0, report)
    return node


def _set_resolution(node, width, height):
    _set(node, "outputresolution", "custom")
    # Keep explicit geometry/output budgets deterministic even when the host
    # project has TouchDesigner's global resolution multiplier enabled.
    _set(node, "resmult", False)
    _set(node, ("resolutionw", "resw"), width)
    _set(node, ("resolutionh", "resh"), height)


def _build_sources(parent, report):
    comp = _ensure(parent, "baseCOMP", "SOURCES", report)
    _style(comp, -1320, 300, (0.42, 0.22, 0.54),
           "Demo now; drop StreamDiffusionTD.tox into its explicit adapter", 250, 115)

    page = _page(comp, "Source")
    use_stream = _custom(comp, page, "Toggle", "UseStreamDiffusion", False,
                         label="Use StreamDiffusion Adapter")
    use_depth = _custom(comp, page, "Toggle", "UseExternalDepth", False,
                        label="Use Adapter Depth")

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
    _out_top(adapter, "OUT_RGB", tox_rgb, 0, report)
    _out_top(adapter, "OUT_DEPTH", tox_depth, 1, report)
    _table(adapter, "ADAPTER_CONTRACT", [
        ["output", "required contract", "replace node"],
        ["OUT_RGB", TOP_CONTRACTS["RGB"], "REPLACE_WITH_STREAMDIFFUSION_RGB"],
        ["OUT_DEPTH", TOP_CONTRACTS["DEPTH"], "REPLACE_WITH_DEPTH_ESTIMATE"],
    ], report)
    _text(adapter, "README_FIRST", "STREAMDIFFUSIONTD ADAPTER BOUNDARY\n\n"
          "Demo mode works without this branch. Later place StreamDiffusionTD.tox here, "
          "wire its image to OUT_RGB, and wire its depth estimate to OUT_DEPTH. "
          "If the TOX emits only RGB, keep DEMO_DEPTH or replace OUT_DEPTH with any depth model. "
          "Do not modify downstream POSITION/COLOR contracts.", report)

    rgb_switch = _ensure(comp, "switchTOP", "RGB_SOURCE", report)
    depth_switch = _ensure(comp, "switchTOP", "DEPTH_SOURCE", report)
    _connect(demo_rgb, rgb_switch, 0, 0, report)
    _connect(adapter, rgb_switch, 1, 0, report)
    _connect(demo_depth, depth_switch, 0, 0, report)
    _connect(adapter, depth_switch, 1, 1, report)
    stream_name = use_stream.name if use_stream is not None else "Usestreamdiffusion"
    depth_name = use_depth.name if use_depth is not None else "Useexternaldepth"
    _expr(rgb_switch, "index", "1 if parent().par.%s else 0" % stream_name)
    _expr(depth_switch, "index", "1 if parent().par.%s else 0" % depth_name)
    _out_top(comp, "OUT_RGB", rgb_switch, 0, report)
    _out_top(comp, "OUT_DEPTH", depth_switch, 1, report)
    _table(comp, "SOURCE_STATUS", [
        ["mode", "RGB", "depth"],
        ["default", "DEMO_RGB_GENERATOR", "DEMO_DEPTH_GENERATOR"],
        ["future", "STREAMDIFFUSION_ADAPTER/OUT_RGB", "STREAMDIFFUSION_ADAPTER/OUT_DEPTH"],
    ], report)
    return comp


def _build_role_bridge(parent, report):
    """Build an atomic RGB/depth preview bridge for split process roles.

    The sender packs RGB and normalized depth into one RGBA16F TOP before it
    crosses process/machine boundaries, so a receiver cannot combine textures
    from different generation frames. ``local`` bypasses pack/unpack entirely.
    This is a direct image bridge, not the richer WorldBus v1 metadata/control
    protocol implemented by ``src/flexgpu/worldbus.py``.
    """
    comp = _ensure(parent, "baseCOMP", "ROLE_BRIDGE", report)
    _style(comp, -1160, 80, (0.18, 0.38, 0.58),
           "Atomic RGB/depth atlas: local, shared memory, or Touch TCP preview",
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

    atlas_pack = _glsl(comp, "PACK_ATOMIC_ATLAS", "transport_pack_atlas",
                       [local_rgb, local_depth], report)
    _set(atlas_pack, "outputresolution", "custom")
    _set(atlas_pack, "resmult", False)
    _expr(atlas_pack, ("resolutionw", "resw"),
          "max(2, int(parent().par.Atlaswidth.eval()))")
    _expr(atlas_pack, ("resolutionh", "resh"),
          "max(1, int(parent().par.Atlasheight.eval()))")
    _set(atlas_pack, "format", "rgba16float")

    shared_rx = _ensure(comp, "sharedmeminTOP", "RX_SHARED_ATLAS", report,
                        optional=True)
    shared_tx = _ensure(comp, "sharedmemoutTOP", "TX_SHARED_ATLAS", report,
                        optional=True)
    for node in (shared_rx, shared_tx):
        required(node, ("name", "memname"),
                 "str(parent().par.Segmentname.eval()) + '_atlas'", True)
        required(node, "memtype", "global")
        required(node, "format", "rgba16float")
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
    required(tcp_rx, "format", "rgba16float")
    required(tcp_tx, "active", "1 if parent().par.Senderactive.eval() else 0", True)
    # Touch Out calls this parameter fps, but it is frames-per-send step.
    required(tcp_tx, "fps", "max(1, int(parent().par.Sendstep.eval()))", True)
    required(tcp_tx, "videocodec", "uncompressed")
    required(tcp_tx, "alwayscook", True)
    required(tcp_tx, "port", "int(parent().par.Atlasport.eval())", True)
    required(tcp_tx, "format", "rgba16float")
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
    for node in (unpack_rgb, unpack_depth):
        _set(node, "outputresolution", "custom")
        _set(node, "resmult", False)
        _expr(node, ("resolutionw", "resw"),
              "max(1, int(parent().par.Atlaswidth.eval()) // 2)")
        _expr(node, ("resolutionh", "resh"),
              "max(1, int(parent().par.Atlasheight.eval()))")
    _set(unpack_rgb, "format", "rgba16float")
    _set(unpack_depth, "format", "mono16float")

    rgb_route = _ensure(comp, "switchTOP", "RGB_ROUTE", report)
    depth_route = _ensure(comp, "switchTOP", "DEPTH_ROUTE", report)
    _connect(local_rgb, rgb_route, 0, 0, report, replace=True)
    _connect(unpack_rgb, rgb_route, 1, 0, report, replace=True)
    _connect(local_depth, depth_route, 0, 0, report, replace=True)
    _connect(unpack_depth, depth_route, 1, 0, report, replace=True)
    _set(rgb_route, "index", 0)
    _set(depth_route, "index", 0)
    _out_top(comp, "OUT_RGB", rgb_route, 0, report)
    _out_top(comp, "OUT_DEPTH", depth_route, 1, report)

    _table(comp, "TRANSPORT_CONTRACT", [
        ["mode", "frame", "endpoint", "contract"],
        ["local", "no copy", "same process", "raw RGB + depth"],
        ["shared_memory", "atomic", "Segmentname_atlas", "RGBA16F: left RGB, right depth"],
        ["touch_tcp", "atomic", "Atlasport", "uncompressed RGBA16F atlas"],
        ["cadence", "Sendfps target", "Sendstep frame modulus", "project.cookRate derived"],
        ["scope", "preview bridge", "no metadata/control", "not WorldBus v1"],
    ], report)
    _text(comp, "README_FIRST", "ROLE-AWARE ATOMIC PREVIEW BRIDGE\n\n"
          "Single topology routes RGB/depth locally without a copy. dual_local "
          "uses one global Shared Mem RGBA16F atlas. dual_network uses one "
          "uncompressed Touch Out/In atlas on Atlasport. Its left half is RGB "
          "and right half is normalized depth, making both textures atomic. "
          "Touch Out's fps parameter is a frame-step value derived from "
          "project.cookRate and Sendfps. The frame-start callback force-cooks "
          "Shared Mem Out at the same step even when world stages are disabled. "
          "This direct preview bridge intentionally omits WorldBus v1 metadata, "
          "camera transforms, heartbeats and controls; use a production WorldBus "
          "adapter when those contracts are required.", report)
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
    page = _page(comp, "Geometry")
    _custom(comp, page, "Int", "Geometryresolution", 384,
            label="Geometry Resolution")
    # Version 1.0.0 created COLOR_ALIGNED as a Null TOP. _ensure() deliberately
    # preserves existing/unknown nodes, so reusing that name cannot migrate its
    # operator type and Common-page resolution values remain ineffective. Keep
    # the legacy node untouched and use an unambiguous managed Resolution TOP.
    color = _ensure(comp, "resolutionTOP", "COLOR_ALIGNED_RESIZE", report)
    _connect(rgb, color, report=report)
    _set(color, "outputresolution", "custom")
    _set(color, "resmult", False)
    _expr(color, ("resolutionw", "resw"), "parent().par.Geometryresolution")
    _expr(color, ("resolutionh", "resh"), "parent().par.Geometryresolution")
    position = _glsl(comp, "depth_to_position", "depth_to_position",
                     [color, depth], report, True)
    # Repair occupied 1.0.0 internal wires: both the shader and OUT_COLOR may
    # still point at the preserved legacy COLOR_ALIGNED Null TOP.
    _connect(color, position, 0, 0, report, replace=True)
    _connect(depth, position, 1, 0, report, replace=True)
    _out_top(comp, "OUT_POSITION", position, 0, report)
    color_out = _out_top(comp, "OUT_COLOR", color, 1, report)
    _connect(color, color_out, 0, 0, report, replace=True)
    _table(comp, "OUTPUT_CONTRACT", [["output", "contract"],
        ["OUT_POSITION", TOP_CONTRACTS["POSITION"]],
        ["OUT_COLOR", TOP_CONTRACTS["COLOR"]]], report)
    return comp


def _build_sensor(parent, report):
    comp = _ensure(parent, "baseCOMP", "SENSOR_INTERACTION", report)
    _style(comp, -730, 300, (0.18, 0.46, 0.34),
           "Animated fallback sensor; later replace at the same TOP contracts", 245, 110)
    position = _in_top(comp, "WORLD_POSITION_IN", 0, report)
    page = _page(comp, "Sensor")
    _custom(comp, page, "Menu", "Mode", "simulated", ("simulated", "replay"))

    circle = _ensure(comp, "circleTOP", "SIMULATED_SENSOR_MASK", report, optional=True)
    if circle is None:
        circle = _ensure(comp, "noiseTOP", "SIMULATED_SENSOR_MASK_FALLBACK", report)
    _set_resolution(circle, 384, 384)
    _set(circle, ("radius", "radius1"), 0.16)
    _expr(circle, ("centerx", "cx", "tx"), "0.5 + 0.24 * math.sin(absTime.seconds * 0.73)")
    _expr(circle, ("centery", "cy", "ty"), "0.5 + 0.18 * math.cos(absTime.seconds * 0.91)")

    replay = _ensure(comp, "baseCOMP", "REPLAY_SENSOR_ADAPTER", report)
    _style(replay, -60, 140, (0.36, 0.33, 0.20),
           "Optional recorded mask/depth source; disabled in simulated mode", 230, 105)
    replay_page = _page(replay, "Adapter")
    _custom(replay, replay_page, "Toggle", "Enabled", False)
    replay_mask = _ensure(replay, "constantTOP", "REPLACE_WITH_REPLAY_MASK", report)
    _set_resolution(replay_mask, 384, 384)
    _set(replay_mask, ("colora", "alpha"), 0.0)
    _out_top(replay, "OUT_MASK", replay_mask, 0, report)

    mask_switch = _ensure(comp, "switchTOP", "SENSOR_MASK", report)
    _connect(circle, mask_switch, 0, 0, report)
    _connect(replay, mask_switch, 1, 0, report)
    _expr(mask_switch, "index", "parent().par.Mode.menuIndex")
    sensor_position = _glsl(comp, "sensor_position", "sensor_position", [mask_switch], report, True)
    interaction = _glsl(comp, "interaction_field", "interaction_field",
                        [position, mask_switch], report, False)
    _out_top(comp, "OUT_SENSOR_POSITION", sensor_position, 0, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 1, report)
    _out_top(comp, "OUT_SENSOR_MASK", mask_switch, 2, report)
    return comp


def _build_persistence(parent, report):
    comp = _ensure(parent, "baseCOMP", "TEMPORAL_WORLD", report)
    _style(comp, -450, 300, (0.16, 0.42, 0.42),
           "GPU feedback: carries old points and applies sensor forces", 230, 110)
    position = _in_top(comp, "POSITION_IN", 0, report)
    color = _in_top(comp, "COLOR_IN", 1, report)
    interaction = _in_top(comp, "INTERACTION_IN", 2, report)
    feedback = _ensure(comp, "feedbackTOP", "POSITION_HISTORY", report)
    # Feedback TOP still needs a seed input even when its target is set.  The
    # live frame is the deterministic first-frame seed; subsequent frames come
    # from the target TOP below.
    _connect(position, feedback, 0, 0, report)
    persistent = _glsl(comp, "temporal_persistence", "temporal_persistence",
                       [position, feedback, interaction], report, True)
    _set(feedback, ("targettop", "target"), persistent.path)
    shader_info = _ensure(comp, "infoDAT", "TEMPORAL_SHADER_INFO", report, optional=True)
    if shader_info is not None:
        _set(shader_info, ("op", "operator"), persistent.path)
    color_hold = _ensure(comp, "nullTOP", "PERSISTENT_COLOR", report)
    _connect(color, color_hold, report=report)
    _out_top(comp, "OUT_POSITION", persistent, 0, report)
    _out_top(comp, "OUT_COLOR", color_hold, 1, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 2, report)
    _text(comp, "RESET_NOTE", "Pulse POSITION_HISTORY Reset after changing source resolution or calibration.", report)
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
        _connect(source, position_switch, index, 0, report)
    for index, source in enumerate((fog_color, procedural_color, hybrid_color)):
        _connect(source, color_switch, index, 0, report)
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
    _out_top(comp, "OUT_POSITION", position, 0, report)
    _out_top(comp, "OUT_COLOR", color, 1, report)
    _out_top(comp, "OUT_INTERACTION", interaction, 2, report)
    _table(comp, "TOP_CONTRACTS", [["name", "contract"]] +
           [[name, TOP_CONTRACTS[name]] for name in
            ("POSITION", "COLOR", "INTERACTION")], report)
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

    points = _ensure(comp, "toptoPOP", "POSITION_TO_POINTS", report, optional=True)
    point_material = _ensure(comp, "pointspriteMAT", "POINT_SPRITE_MATERIAL",
                             report, optional=True)
    if point_material is not None:
        _expr(point_material, "pointsize", "parent().par.Pointsize")
        _set(point_material, "colormap", color.path)
    render_center = None
    render_left = None
    render_right = None
    if points is not None:
        _set(points, "rgba", "pactive")
        _set(points, "input0top", position.path)
        _set(points, "input0chanscope", "r g b a")
        _set(points, "input0attrscope", "P P P P")
        _set(points, "input0filter", "nearest")
        _set(points, "surftype", "points")
        _set(points, "texture", "point")
        _set(points, "maxpointsenable", True)
        _expr(points, "maxpoints", "parent().par.Maxpoints")

        def make_render(name, eye_offset):
            node = _ensure(comp, "rendersimpleTOP", name, report, optional=True)
            if node is None:
                return None
            _set(node, "pop", points.path)
            _set(node, "colormap", color.path)
            if point_material is not None:
                _set(node, "materialsource", "matnode")
                _set(node, "mat", point_material.path)
            else:
                _set(node, "materialsource", "internalphong")
            _set(node, "normalizegeo", True)
            _set(node, "ortho", False)
            _set(node, "fov", 55.0)
            _set(node, "camdistance", 3.0)
            _set(node, "geotranslatex", eye_offset)
            _set(node, "georotatey", eye_offset * -35.0)
            _set(node, "bgcolorr", 0.005)
            _set(node, "bgcolorg", 0.009)
            _set(node, "bgcolorb", 0.018)
            _set(node, "bgcolora", 1.0)
            _set(node, "diffuser", 0.90)
            _set(node, "diffuseg", 0.95)
            _set(node, "diffuseb", 1.0)
            _set_resolution(node, 1280, 720)
            return node

        render_center = make_render("RENDER_CENTER", 0.0)
        render_left = make_render("RENDER_LEFT_EYE", -0.035)
        render_right = make_render("RENDER_RIGHT_EYE", 0.035)

    # A valid color TOP fallback makes the project inspectable even if opened in
    # a pre-POP TouchDesigner build.  In 2025.32820 the switches select renders.
    center_switch = _ensure(comp, "switchTOP", "CENTER_OR_FALLBACK", report)
    left_switch = _ensure(comp, "switchTOP", "LEFT_OR_FALLBACK", report)
    right_switch = _ensure(comp, "switchTOP", "RIGHT_OR_FALLBACK", report)
    for switch, rendered in ((center_switch, render_center),
                             (left_switch, render_left),
                             (right_switch, render_right)):
        _connect(color, switch, 0, 0, report)
        if rendered is not None:
            _connect(rendered, switch, 1, 0, report)
            _set(switch, "index", 1)
        else:
            _set(switch, "index", 0)
    _out_top(comp, "OUT_CENTER", center_switch, 0, report)
    _out_top(comp, "OUT_LEFT_EYE", left_switch, 1, report)
    _out_top(comp, "OUT_RIGHT_EYE", right_switch, 2, report)
    _table(comp, "RENDER_PATH", [
        ["stage", "operator", "contract"],
        ["unpack", "POSITION_TO_POINTS (TOP to POP)", "RGBA = Position and Active"],
        ["thickness", "POINT_SPRITE_MATERIAL", "Pointsize parameter; 3 px default"],
        ["center", "RENDER_CENTER (Render Simple TOP)", TOP_CONTRACTS["INSTALLATION"]],
        ["stereo", "RENDER_LEFT_EYE / RENDER_RIGHT_EYE", TOP_CONTRACTS["STEREO"]],
        ["fallback", "*_OR_FALLBACK input 0", "completed color TOP"],
    ], report)
    return comp


def _build_installation(parent, report):
    comp = _ensure(parent, "baseCOMP", "INSTALLATION_OUTPUT", report)
    _style(comp, 690, 430, (0.46, 0.48, 0.14),
           "Visually inspectable point render plus disocclusion fog plate", 255, 110)
    point_render = _in_top(comp, "POINT_RENDER_IN", 0, report)
    fog_plate = _in_top(comp, "FOG_PLATE_IN", 1, report)
    grade = _glsl(comp, "installation_grade", "installation_grade",
                  [point_render, fog_plate], report, False)
    _set_resolution(grade, 1280, 720)
    output = _ensure(comp, "nullTOP", "OUT_INSTALLATION", report)
    _connect(grade, output, report=report)
    _out_top(comp, "out1", output, 0, report)
    return comp


def _build_stereo(parent, report):
    comp = _ensure(parent, "baseCOMP", "STEREO_PREVIEW", report)
    _style(comp, 690, 250, (0.39, 0.46, 0.18),
           "Desktop left/right/SBS preview; no OpenVR dependency", 245, 105)
    left = _in_top(comp, "LEFT_IN", 0, report)
    right = _in_top(comp, "RIGHT_IN", 1, report)
    layout = _ensure(comp, "layoutTOP", "STEREO_SIDE_BY_SIDE", report, optional=True)
    if layout is None:
        layout = _ensure(comp, "compositeTOP", "STEREO_SIDE_BY_SIDE_FALLBACK", report)
    _connect(left, layout, 0, 0, report)
    _connect(right, layout, 1, 0, report)
    _set(layout, ("align", "direction"), "horizontal")
    _set_resolution(layout, 2560, 720)
    _out_top(comp, "OUT_LEFT_EYE", left, 0, report)
    _out_top(comp, "OUT_RIGHT_EYE", right, 1, report)
    _out_top(comp, "OUT_STEREO_SBS", layout, 2, report)
    _text(comp, "README_FIRST", "This is a headset-independent stereo preview. "
          "A future OpenXR/OpenVR adapter should consume the same point world and "
          "replace only the camera/output layer.", report)
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
            _connect(info, merge, index, 0, report)
        metrics = _ensure(comp, "nullCHOP", "OUT_PERFORMANCE", report, optional=True)
        _connect(merge, metrics, report=report)
        out = _ensure(comp, "outCHOP", "out1", report, optional=True)
        _connect(metrics, out, report=report)
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
    _connect(role_bridge, reconstruction, 0, 0, report, replace=True)
    _connect(role_bridge, reconstruction, 1, 1, report, replace=True)
    _connect(reconstruction, sensor, 0, 0, report, replace=True)
    _connect(reconstruction, temporal, 0, 0, report, replace=True)
    _connect(reconstruction, temporal, 1, 1, report, replace=True)
    _connect(sensor, temporal, 2, 1, report, replace=True)
    _connect(temporal, completion, 0, 0, report, replace=True)
    _connect(temporal, completion, 1, 1, report, replace=True)
    _connect(temporal, completion, 2, 2, report, replace=True)
    _connect(completion, contract, 0, 0, report, replace=True)
    _connect(completion, contract, 1, 1, report, replace=True)
    _connect(sensor, contract, 2, 1, report, replace=True)
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
        ["role_bridge", "ROLE_BRIDGE: local/shared_memory/touch_tcp RGB+depth"],
        ["position_contract", TOP_CONTRACTS["POSITION"]],
        ["renderer", "TOP to POP -> Render Simple TOP (TouchDesigner 2025)"],
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
          "same RGB/depth contracts in split roles. SHARP/Gaussian stubs remain "
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
