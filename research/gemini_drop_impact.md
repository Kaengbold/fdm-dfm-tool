# Drop / impact factor (Gemini research, checked)

2026-11-16, for T-410. Drafted by Gemini Flash in two questions, checked and condensed by Claude.

## The formula

A mass dropped from height h onto an elastic structure that bends δ under the same load held still hits with
n = 1 + √(1 + 2h/δ) times its weight (energy method: W(h + δ_max) = ½kδ_max²; Hibbeler, *Mechanics of
Materials*, impact loading; Gere, *Mechanics of Materials*). `strength.impact_factor` already used this.

Assumptions: rigid falling mass, no energy lost (no damping, no plastic deformation), massless structure, no
local contact deformation. When δ is very small, n ≈ √(2h/δ) and runs away. That is the audit's L-bracket:
δ = 0.04 mm, h = 50 mm → n = 51.

## What else gives: springs in series

Everything the load passes through deflects under it, and the deflections add (series springs):
δ_total = δ_part + δ_mount + δ_contact/object, n = 1 + √(1 + 2h/δ_total). The full peak force still passes
through the part, so σ_part = n · σ_static. Worked: δ_part = 0.04 mm, h = 50 mm → n = 51 (nothing else
gives), 14.7 (+0.5 mm), 8.1 (+2 mm).

Energy lost to damping or plastic deformation lowers n further (roughly by √ of the share that stays elastic).
It's left out here, so the numbers stay on the harsh side.

## Stiffness of the rest (defaults in thresholds.yaml `drop`)

| What | Stiffness | Under 5 kg (49 N) | Source |
|---|---|---|---|
| Screw into wood (slip) | ≈ 1500 N/mm for a 4 mm screw | 0.03 mm | EN 1995-1-1 (Eurocode 5) K_ser = ρ^1.5 · d / 23 (ρ ≈ 420 kg/m³) |
| Hard object on PLA/PETG (Hertz contact, R 10–25 mm) | 1000–3000 N/mm | 0.02–0.05 mm | Hertz contact, E ≈ 3 GPa (estimate) |
| Hard end used (the two above in series) | **1000 N/mm** | 0.05 mm | |
| Rubber feet, Shore 50–70A, 2–5 mm | 25–100 N/mm | 0.5–2 mm | estimate, no measured source |
| Padded bag | 5–25 N/mm | 2–10 mm | estimate |
| Soft end used | **25 N/mm** | 2 mm | estimate |

Both are editable in All settings. A stiffness (not a fixed mm) keeps the give proportional to the load.

## Result for the L-bracket (5 kg, 50 mm)

The part bends 0.04 mm (safety ×19.5 held still): it now hits 8–35× as hard, safety ×0.6–×2.4, "a soft thing
is fine, a hard one may break it", instead of ×51 and "it will likely break".

## Sources

- R. C. Hibbeler, Mechanics of Materials, chapter on impact loading (energy method).
- EN 1995-1-1:2004 (Eurocode 5), 7.1, slip modulus K_ser for screws: https://www.swedishwood.com/building-with-wood/structural-design-eurocode-5/connections/types-of-fasteners/screws/
- Series springs: https://en.wikipedia.org/wiki/Series_and_parallel_springs
- Coefficient of restitution (energy loss, not used): https://en.wikipedia.org/wiki/Coefficient_of_restitution
