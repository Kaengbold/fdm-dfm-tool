# Feature test part

`web/samples/feature_test.stl` ("Feature test" under the samples), made by `tools/make_samples.py`.
One part with something for every check and design change, to test the app by hand. Set the
level to **Advanced** (or Expert) and keep it **lying flat as modelled** (Option 2 in "On the bed").

| Where on the part | What to try | What should happen |
|---|---|---|
| The whole base (100 × 60 mm, sharp corners) | Material ABS, then PLA | Warping: ABS lifts, PLA hardly. Design changes: 4 round bed corners, 0.5 mm bottom chamfer. Elephant foot. |
| Block on the left, sideways Ø8 hole | Problems, Design changes | "Sideways hole prints oval"; teardrop offered. |
| Stem with a 24 mm cap (left middle) | Will it hold?: hold the base's bottom, load the cap sideways | Weak spot at the stem's foot; Make it stronger offers a thicker stem and rounds; ledge under the cap: 45° fill offered, support marks. |
| Two pillars with a beam (front right) | Problems | 30 mm bridge: sags (PLA) / long bridge (ABS). |
| Ø3 hole, Ø5 hole, hex pocket (front left) | Tolerances & tuning → Holes | Ø3 is M3 size: clearance / pilot / insert advice. Hex pocket: M3 nut with no play ("nut trap tight"). |
| Two fins (back middle): 0.6 and 0.3 mm | Problems, Design changes | 0.3 mm too thin to print; both offered thicker. |
| Two wall pairs (back right): 3 mm and 0.15 mm gap | Tolerances & tuning → Slots and flat gaps: pick the 3 mm slot's faces | 0.15 mm gap fuses shut. The 3 mm slot gets a fit and a width to model. |
| Thin pin, 40 mm tall (middle) | While printing → Cooling; Problems | Small layers don't cool in time (heat map red at the top); weak upright pin; tall and thin. |
| Four engraved lines (back left, 0.6 mm deep) | Problems | Shallow relief. |
| Anywhere | Advanced design changes | Support / no support boxes on the cap's underside; a hidden gap clicked on the block's side goes into the wall. |
| — | Download | 3MF with the settings; report with pictures. |
