/**
 * Node entry for the installable Bundle package.
 * The effective composition lives in `cordis.patch.yml`; this entry exists so
 * the package follows the same built-artifact shape as shipped dsh bundles.
 */
export const name = 'wren-data-agent-bundle'

/** Bundle packages are resolved through their manifest patch, not mounted as a plugin row. */
export function apply(): void {
  // Intentionally empty.
}
