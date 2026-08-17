/**
 * Host-visible half of this browser-only plugin.
 *
 * Harness imports the package root while assembling the Cordis tree.  Browser
 * code is discovered separately through the `./client` export and must not be
 * imported here (it depends on the web platform module table).
 */
export function apply(): void {}
