# Extra simulations: which ones are worth it (T-242)

Quick indicators only: each has to run in seconds in the browser, use inputs a hobbyist knows,
and change what they do (material, vents, a fan, a thicker part). Decided 2026-10-31.

## Heat inside an electronics box: yes (built)

Common hobby use (Raspberry Pi / ESP32 / Arduino cases, LED drivers), and the answer changes the
material choice: a closed PLA box around a few watts gets warm enough to soften.

Model: all the power ends up as heat leaving through the outside surface,
ΔT = P / (U · A), with U the overall heat transfer of the wall to still room air.

- U ≈ 4.6 W/m²K for a sealed box in still air: Elliott Electric's sealed-enclosure chart, 300 W on
  42 ft² (7.1 W/ft²) gives a 16.7 °C rise
  (https://elliottelectric.com/StaticPages/ElectricalReferences/Guides/Heat_Dissipation_Electrical_Enclosures.aspx).
  Firgelli gives 3–6 W/m²K for natural convection, 20–40 with forced air
  (https://www.firgelliauto.com/blogs/engineering-calculators/watts-to-heat-calculator).
  With a fan we use 20 (the low end).
- The printed wall in series: plastic conducts about 0.2 W/m·K; a 2 mm wall adds ~7 %. Metal-box
  charts slightly overstate how well a plastic box sheds heat; the wall term covers most of that.
- A: outside surface as a prism around the outline along the tightest axis (exact for boxes and
  cylinders; inner walls don't count).
- Verdict against the same limits as "Where is it used?" (softening point, heat limit), and the
  inside temperature can be handed to those checks with one click.

Not modelled: vents (the result lies between closed and fan; how much depends on their size and
where they are), sunlight on the box, hot spots right next to a chip.

`api.box_heat`, card in Optional simulations.

## Creep under a constant load: no new simulation

Already covered: "the load is on all the time" asks for ×3 safety instead of ×2, and warmth lowers
strength (`service.heat_factor`). A real creep curve (strain over months) needs creep data per
material and temperature (e.g. Findley power-law fits); filament makers don't publish it, and
printed parts creep differently along and across layers. Without that data a curve would look
precise and be a guess. Revisit if calibration prints for creep are added (a hook with a weight,
measured over weeks).

## Wear: no

Wear depends on the mating part (material, roughness, hardness), sliding speed, pressure and
lubrication more than on the printed part; published wear rates for printed plastics vary by
orders of magnitude between test setups. A number we'd show can't be trusted. Better: the
material advisor's wording (nylon / PA-CF for sliding parts) and design tips (replaceable bushings,
a metal pin in a printed hole).
