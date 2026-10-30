---
version: 1
slug: "web-index-html"
primary_target: "web/index.html"
related_targets: ["web/style.css","web/app.js"]
---

Scope: the whole web app (web/index.html, style.css, app.js, steps.js). Mode: Operate.
Audience and job: hobbyists checking one part before printing; task = load part, pick printer and material, read problems, download a 3MF.
Constraints: keep every engine feature and element id the browser test uses; Simple and Basic must work on phones; Expert (one long page) later.

## Direction contract
THESIS: A workbench, not a form. Three fixed zones: a step list that reads like a checklist, the part large in the middle, one step's controls on the right. Refuses the long scrolling sidebar of every slicer plugin.
OWN-WORLD: Light, near-white panels with a faint green tint (#f3f5f2, #fdfdfc), dark green ink and buttons (#1f5c47, text #1d2b27), signal yellow (#f2c12e) only for "you are here" and focus. Atkinson Hyperlegible. Small radii (6px), hairline borders, no gradients. The 3D view on a calm light grey, no mat.
STORY: The user sees where they are, what is running and what a change will update; detail opens only on request.
FIRST VIEWPORT: Header with name and the Simple / Basic / Advanced switch. Left rail with numbered steps. Centre: 3D view. Right: the current step's panel with one primary button at its foot.
FORM: user-pinned (mockup A2, look "cutting mat" without the mat). seed: none (unavailable)
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
