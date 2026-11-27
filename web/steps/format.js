// Shared number formatters for Options, Problems, Simulation and Done (T-632).
// Pure module (no DOM imports) so Node unit tests can import it directly.

/** Safety factor to one decimal place ("3.9", "4.0"), or null when absent. */
export function formatSafety(value) {
  if (value == null || !Number.isFinite(Number(value))) return null;
  return Number(value).toFixed(1);
}

/** Safety range as {low, high} formatted with formatSafety, or null when absent or both ends round to the same value. */
export function formatSafetyRange(range) {
  if (!Array.isArray(range) || range.length < 2) return null;
  const low = formatSafety(range[0]);
  const high = formatSafety(range[1]);
  return low == null || high == null || low === high ? null : { low, high };
}

/** Layer height to two decimal places ("0.20"), or null when absent. */
export function formatLayerHeight(value) {
  if (value == null || !Number.isFinite(Number(value))) return null;
  return Number(value).toFixed(2);
}
