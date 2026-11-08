# FDM DFM Tool for Fusion: check a body for 3D printing with the same engine as the web app,
# show the report in a palette, and add 45° teardrops to sideways holes as real CAD features.
# Licence: AGPL-3.0-or-later. Engine logic lives in bridge.py (no adsk, tested outside Fusion).
import os
import tempfile
import traceback

import adsk.core
import adsk.fusion

from . import bridge

CMD_ID = "fdmdfm_check"
PALETTE_ID = "fdmdfm_report"
PANEL_ID = "SolidScriptsAddinsPanel"  # Utilities > Add-ins panel of the Design workspace
GOALS = ["balanced", "strength", "speed", "material"]

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []  # keep event handlers alive


def run(context):
    try:
        cmd = ui.commandDefinitions.itemById(CMD_ID) or ui.commandDefinitions.addButtonDefinition(
            CMD_ID, "Check for 3D printing", "Best print orientation, printability problems and slicer settings (FDM DFM Tool).", "")
        on_created = CommandCreated()
        cmd.commandCreated.add(on_created)
        handlers.append(on_created)
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd)
    except Exception:
        ui.messageBox(f"FDM DFM Tool failed to start:\n{traceback.format_exc()}")


def stop(context):
    panel = ui.allToolbarPanels.itemById(PANEL_ID)
    control = panel.controls.itemById(CMD_ID) if panel else None
    if control:
        control.deleteMe()
    cmd = ui.commandDefinitions.itemById(CMD_ID)
    if cmd:
        cmd.deleteMe()
    palette = ui.palettes.itemById(PALETTE_ID)
    if palette:
        palette.deleteMe()


class CommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            body = inputs.addSelectionInput("body", "Body", "Select the solid body to check")
            body.addSelectionFilter("SolidBodies")
            body.setSelectionLimits(1, 1)
            p = bridge.presets()
            for key, label, options, default in (("printer", "Printer", p["printers"], "bambu_p1s"),
                                                 ("material", "Material", p["materials"], "pla"),
                                                 ("goal", "Optimise for", {g: g.capitalize() for g in GOALS}, "balanced")):
                drop = inputs.addDropDownCommandInput(key, label, adsk.core.DropDownStyles.TextListDropDownStyle)
                for value, name in options.items():
                    drop.listItems.add(name, value == default, "")
            inputs.addBoolValueInput("teardrops", "Add teardrops to sideways holes", True, "", False)
            on_execute = CommandExecute()
            args.command.execute.add(on_execute)
            handlers.append(on_execute)
        except Exception:
            ui.messageBox(f"FDM DFM Tool:\n{traceback.format_exc()}")


class CommandExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            body = adsk.fusion.BRepBody.cast(inputs.itemById("body").selection(0).entity)
            choice = {k: _selected_key(inputs.itemById(k)) for k in ("printer", "material", "goal")}
            result = bridge.analyse(_export_stl(body), choice["printer"], choice["material"], choice["goal"], body.name)
            _show_report(bridge.report_html(result["report"], result["up"]))
            if inputs.itemById("teardrops").value and result["teardrops"]:
                for hole in result["teardrops"]:
                    _add_teardrop(body, hole, result["up"])
                ui.messageBox(f"Added {len(result['teardrops'])} teardrop(s). Print with [{', '.join(f'{v:.2f}' for v in result['up'])}] up.")
        except Exception:
            ui.messageBox(f"FDM DFM Tool:\n{traceback.format_exc()}")


def _selected_key(drop):
    """Dropdowns show names; map the selected name back to its preset id."""
    p = bridge.presets()
    table = {"printer": p["printers"], "material": p["materials"], "goal": {g: g.capitalize() for g in GOALS}}[drop.id]
    return next(k for k, v in table.items() if v == drop.selectedItem.name)


def _export_stl(body) -> bytes:
    if body.assemblyContext:
        raise ValueError("Pick a body of the top-level component (bodies inside other components aren't supported yet).")
    with tempfile.TemporaryDirectory(prefix="fdmdfm-") as folder:  # removed again after reading
        path = os.path.join(folder, "part.stl")
        design = adsk.fusion.Design.cast(app.activeProduct)
        options = design.exportManager.createSTLExportOptions(body, path)
        options.isBinaryFormat = True
        options.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
        options.unitType = adsk.fusion.DistanceUnits.MillimeterDistanceUnits
        if not design.exportManager.execute(options):
            raise RuntimeError("STL export failed")
        with open(path, "rb") as f:
            return f.read()


_report_dir = None


def _show_report(page: str):
    # A private folder of our own (not a fixed name in the shared temp folder another program could take over).
    global _report_dir
    if _report_dir is None or not os.path.isdir(_report_dir):
        _report_dir = tempfile.mkdtemp(prefix="fdmdfm-report-")
    path = os.path.join(_report_dir, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    palette = ui.palettes.itemById(PALETTE_ID)
    if palette:
        palette.deleteMe()
    palette = ui.palettes.add(PALETTE_ID, "3D print check", path, True, True, True, 420, 600)
    palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight


def _add_teardrop(body, hole: dict, up):
    """Cut the 45° roof of a teardrop into a sideways hole: two temporary boxes intersected, then
    added as a base feature and cut from the body, so it shows in the timeline."""
    cm = 0.1  # the engine works in mm, Fusion in cm
    tbm = adsk.fusion.TemporaryBRepManager.get()
    boxes = []
    pair = bridge.teardrop_boxes(hole, up)
    if pair is None:  # runs along up: prints round as it is
        return
    for b in pair:
        boxes.append(tbm.createBox(adsk.core.OrientedBoundingBox3D.create(
            adsk.core.Point3D.create(*(v * cm for v in b["center"])),
            adsk.core.Vector3D.create(*b["length_dir"]), adsk.core.Vector3D.create(*b["width_dir"]),
            b["length"] * cm, b["width"] * cm, b["height"] * cm)))
    roof = boxes[0]
    tbm.booleanOperation(roof, boxes[1], adsk.fusion.BooleanTypes.IntersectionBooleanType)
    design = adsk.fusion.Design.cast(app.activeProduct)
    comp = body.parentComponent
    if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
        base = comp.features.baseFeatures.add()
        base.startEdit()
        comp.bRepBodies.add(roof, base)
        base.finishEdit()
        tool = base.bodies.item(0)
    else:
        tool = comp.bRepBodies.add(roof)
    tools = adsk.core.ObjectCollection.create()
    tools.add(tool)
    cut = comp.features.combineFeatures.createInput(body, tools)
    cut.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    comp.features.combineFeatures.add(cut)
