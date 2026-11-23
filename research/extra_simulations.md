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

## Creep under a constant load

T-343 adds a calibration print for a user's own filament. The arm sits flat in the printer, then
clamps to a table by its wide end. A 500 g load hangs 75 mm from the clamp and the test section is
15 mm wide by 4 mm thick. The nominal bending stress at the clamp is
`6 × (0.5 kg × 9.80665 m/s²) × 75 mm / (15 mm × (4 mm)²) = 9.19 MPa`.
The lower edge of the pointed tip can be read against a fixed ruler. Record its drop from unloaded
after 1 minute, 1 hour, 1 day, 1 week and 4 weeks with the weight left in place.

The app fits drop against log(time), anchored at the 1-minute reading, with a nonnegative slope.
For steady loads at 18–30 °C, the strength check multiplies its usual ×2 margin by the predicted
365-day drop divided by the initial drop. The optimizer also keeps the goal's minimum margin.
The ×3 fallback applies outside that temperature range or until the initial reading and a reading
from at least 1 hour are available. An estimate from fewer than four readings or less than
four weeks is marked provisional.

This is a sag-based margin, with extrapolation beyond the last reading. Print settings, load
direction, temperature, and stress can all change how the plastic creeps. Estimating time to
rupture requires separate failure testing.

## Wear: no

Wear depends on the mating part (material, roughness, hardness), sliding speed, pressure and
lubrication more than on the printed part; published wear rates for printed plastics vary by
orders of magnitude between test setups. A number we'd show can't be trusted. Better: the
material advisor's wording (nylon / PA-CF for sliding parts) and design tips (replaceable bushings,
a metal pin in a printed hole).
