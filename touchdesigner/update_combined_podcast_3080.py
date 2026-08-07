"""Refresh the public Recovered Homes controls in a combined 3080 TOE.

This module deliberately treats ``STREAMDIFFUSION_ADAPTER`` as a black-box
integration boundary.  It updates only the Recovered Homes managed controls,
callback DATs, portable media/module paths, and marked color/audio/Spout
helpers.  It never saves the project, starts a model server, replaces the
private StreamDiffusion component, or changes any 5090 project.

Run :func:`update_live_combined_podcast_3080` from the TouchDesigner Textport
with the local home-podcast checkout path after saving a rollback TOE.
"""

from importlib import util
from pathlib import Path
import builtins
import re
import sys


ADAPTER_PATH = (
    "/project1/flexgpu/WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER"
)
PROJECT_NAME_PATTERN = re.compile(
    r"FlexShow.*podcast.*3080(?:\.\d+)?\.toe",
    re.IGNORECASE,
)
VISUAL_PATH_NAMES = ("original", "human_figures")
VISUAL_PATH_LABELS = ("Original Story Visuals", "Human Figures")


def _required_paths(podcast_root):
    root = Path(podcast_root).expanduser().resolve()
    episode = root / "episodes" / "2013-12.01"
    return root, {
        "show_control": root / "touchdesigner" / "show_control_component.py",
        "show_callbacks": root / "touchdesigner" / "show_control_callbacks.py",
        "execute_callbacks": root / "touchdesigner" / "execute_callbacks.py",
        "parameter_callbacks": root / "touchdesigner" / "parameter_callbacks.py",
        "sequencer": root / "touchdesigner" / "podcast_sequencer.py",
        "controller": root / "touchdesigner" / "podcast_td_controller.py",
        "scene": (
            episode / "visuals" / "2013-12.01-visual-scenes.json"
        ),
        "human_scene": (
            episode
            / "visuals"
            / "2013-12.01-visual-scenes-human-figures.json"
        ),
        "voices_audio": episode / "audio" / "2013-12.01-voices-only.mp3",
        "soundscape_audio": (
            episode / "audio" / "2013-12.01-soundscape-only.mp3"
        ),
    }


def _load_module(path, module_name):
    spec = util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load public module: %s" % path)
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _parameter(node, name):
    try:
        return getattr(node.par, name)
    except Exception:
        return None


def _set_parameter(node, name, value):
    parameter = _parameter(node, name)
    if parameter is None:
        raise RuntimeError("%s is missing parameter %s" % (node.path, name))
    parameter.val = value


def _custom_page(node, name):
    page = next(
        (
            candidate
            for candidate in getattr(node, "customPages", ())
            if candidate.name == name
        ),
        None,
    )
    return page if page is not None else node.appendCustomPage(name)


def _ensure_file_parameter(node, page, name, label):
    parameter = _parameter(node, name)
    if parameter is None:
        parameter = page.appendFile(name, label=label)[0]
    return parameter


def _ensure_menu_parameter(node, page, name, label, names, labels):
    parameter = _parameter(node, name)
    if parameter is None:
        parameter = page.appendMenu(name, label=label)[0]
    parameter.menuNames = list(names)
    parameter.menuLabels = list(labels)
    return parameter


def _connected(source, target):
    try:
        return target in source.outputs
    except Exception:
        return False


def update_combined_podcast_3080(
        adapter, podcast_root, *, operator_types, project_name):
    """Apply the public dual-prompt update to one verified combined adapter."""

    project_name = Path(str(project_name)).name
    if PROJECT_NAME_PATTERN.fullmatch(project_name) is None:
        raise RuntimeError(
            "Refusing to update a non-3080 combined podcast project: %s"
            % project_name
        )
    if adapter is None or str(getattr(adapter, "path", "")) != ADAPTER_PATH:
        raise RuntimeError(
            "Missing the approved combined podcast adapter: %s" % ADAPTER_PATH
        )

    root, paths = _required_paths(podcast_root)
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "The home-podcast checkout is incomplete: " + ", ".join(missing)
        )

    stream_parameter = _parameter(adapter, "Streamdiffusionpath")
    if stream_parameter is None:
        raise RuntimeError(
            "%s is missing the public Streamdiffusionpath contract"
            % ADAPTER_PATH
        )
    streamdiffusion_paths = str(stream_parameter.eval())

    preflight_children = (
        "show_control",
        "execute_callbacks",
        "parameter_callbacks",
    )
    missing_children = [
        name for name in preflight_children if adapter.op(name) is None
    ]
    if missing_children:
        raise RuntimeError(
            "The combined podcast preflight is missing: "
            + ", ".join(missing_children)
        )

    module = _load_module(
        paths["show_control"],
        "flexgpu_combined_podcast_show_control",
    )
    podcast_page = _custom_page(adapter, "Podcast")
    _ensure_menu_parameter(
        adapter,
        podcast_page,
        "Audiosource",
        "Audio Source",
        module.AUDIO_SOURCE_NAMES,
        module.AUDIO_SOURCE_LABELS,
    )
    _ensure_file_parameter(
        adapter,
        podcast_page,
        "Soundscapeaudiofile",
        "Soundscape-only Audio",
    )
    # The combined FlexGPU TOE already owns its working audio, color, and
    # Spout pipelines. Pass only the types required to rebuild the public
    # control COMP so the home-podcast installer cannot claim or rewire those
    # existing combined-project nodes. Their established connections are
    # verified below instead.
    control_operator_types = {
        name: operator_types[name]
        for name in (
            "baseCOMP",
            "tableDAT",
            "parameterexecuteDAT",
            "op",
        )
    }
    control = module.install_show_control(
        adapter,
        root,
        operator_types=control_operator_types,
    )

    portable_values = {
        "Scenejson": paths["scene"],
        "Humanfigurejson": paths["human_scene"],
        "Audiofile": paths["voices_audio"],
        "Soundscapeaudiofile": paths["soundscape_audio"],
        "Sequencermodule": paths["sequencer"],
        "Controllermodule": paths["controller"],
    }
    for name, path in portable_values.items():
        _set_parameter(adapter, name, str(path))

    callback_sources = {
        "execute_callbacks": paths["execute_callbacks"],
        "parameter_callbacks": paths["parameter_callbacks"],
        "show_control/control_callbacks": paths["show_callbacks"],
    }
    callbacks = {}
    for relative_path, source_path in callback_sources.items():
        dat = adapter.op(relative_path)
        if dat is None:
            raise RuntimeError(
                "The combined podcast adapter is missing %s" % relative_path
            )
        dat.text = source_path.read_text(encoding="utf-8")
        callbacks[relative_path] = dat

    required_connections = (
        ("voices_only_audio", "audiosource_switch"),
        ("soundscape_audio", "audiosource_switch"),
        ("audiosource_switch", "audio_out"),
    )
    for source_name, target_name in required_connections:
        source = adapter.op(source_name)
        target = adapter.op(target_name)
        if source is None or target is None or not _connected(source, target):
            raise RuntimeError(
                "Missing combined podcast connection %s -> %s"
                % (source_name, target_name)
            )

    if str(stream_parameter.eval()) != streamdiffusion_paths:
        raise RuntimeError(
            "The StreamDiffusion operator list changed unexpectedly"
        )

    visual_parameter = _parameter(control, "Visualpath")
    connector_visual = _parameter(adapter, "Visualpath")
    human_scene_parameter = _parameter(adapter, "Humanfigurejson")
    if (
            visual_parameter is None
            or connector_visual is None
            or human_scene_parameter is None):
        raise RuntimeError("The dual visual-path controls were not installed")
    if tuple(visual_parameter.menuNames) != VISUAL_PATH_NAMES:
        raise RuntimeError("The Visual Path menu names are incomplete")
    if tuple(visual_parameter.menuLabels) != VISUAL_PATH_LABELS:
        raise RuntimeError("The Visual Path menu labels are incomplete")

    controller = callbacks["execute_callbacks"].module.get_controller()
    controller.reload()
    if bool(_parameter(adapter, "Enabled").eval()):
        controller.update(float(_parameter(adapter, "Playheadsec").eval()))

    adapter.current = True
    return {
        "status": "updated",
        "project": project_name,
        "project_variant": "3080",
        "adapter": adapter.path,
        "podcast_root": str(root),
        "visual_path_menu": list(visual_parameter.menuNames),
        "visual_path": str(visual_parameter.eval()),
        "original_scene_json": str(_parameter(adapter, "Scenejson").eval()),
        "human_figure_scene_json": str(human_scene_parameter.eval()),
        "streamdiffusion_paths": streamdiffusion_paths,
        "saved": False,
        "model_servers_started": False,
    }


def update_live_combined_podcast_3080(podcast_root):
    """Resolve TouchDesigner globals lazily and update the active 3080 TOE."""

    try:
        import td  # type: ignore
    except ImportError as error:
        raise RuntimeError(
            "Run update_live_combined_podcast_3080 inside TouchDesigner"
        ) from error

    main_namespace = vars(sys.modules.get("__main__"))

    def symbol(name):
        value = getattr(td, name, None)
        if value is None:
            value = main_namespace.get(name)
        if value is None:
            value = getattr(builtins, name, None)
        if value is None:
            raise RuntimeError("TouchDesigner did not expose %s" % name)
        return value

    operator_types = {
        name: symbol(name)
        for name in (
            "baseCOMP",
            "tableDAT",
            "parameterexecuteDAT",
        )
    }
    operator_lookup = symbol("op")
    operator_types["op"] = operator_lookup
    adapter = operator_lookup(ADAPTER_PATH)
    return update_combined_podcast_3080(
        adapter,
        podcast_root,
        operator_types=operator_types,
        project_name=symbol("project").name,
    )


if __name__ == "__main__":
    raise SystemExit(
        "Import this module inside TouchDesigner; it has no command-line mode."
    )
