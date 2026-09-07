import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const sourceRoot = resolve(process.cwd(), "src");
const consoleCss = readFileSync(resolve(sourceRoot, "styles.css"), "utf8");
const accessCss = readFileSync(resolve(sourceRoot, "components/access-control.css"), "utf8");
const cubeCss = readFileSync(resolve(sourceRoot, "components/cube-workbench.css"), "utf8");
const knowledgeCss = readFileSync(resolve(sourceRoot, "components/knowledge-workbench.css"), "utf8");
const viewCss = readFileSync(resolve(sourceRoot, "components/view-workbench.css"), "utf8");

function rule(css: string, selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return Array.from(css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`, "g")), (match) => match[1]).join("\n");
}

describe("responsive layout contracts", () => {
  it("bounds every primary master-detail workbench", () => {
    expect(consoleCss).toContain("--workbench-height:");
    expect(rule(consoleCss, ".datasources-layout")).toContain("height: var(--workbench-height)");
    expect(rule(consoleCss, ".schema-browser")).toContain("height: var(--workbench-height)");
    expect(rule(consoleCss, ".mdl-layout")).toContain("height: var(--workbench-height)");
    expect(rule(knowledgeCss, ".kw-rules-workbench")).toContain("height: var(--workbench-height)");
    expect(rule(knowledgeCss, ".kw-sql-workbench")).toContain("height: var(--workbench-height)");
    expect(rule(viewCss, ".view-workbench")).toContain("height: var(--workbench-height)");
    expect(rule(cubeCss, ".cube-workbench")).toContain("height: var(--workbench-height)");
    expect(rule(accessCss, ".access-account-layout")).toContain("height: var(--workbench-height)");
  });

  it("keeps lists, details, code, tables, and the low-height sidebar independently scrollable", () => {
    expect(rule(consoleCss, ".sidebar > nav")).toContain("overflow-y: auto");
    expect(rule(consoleCss, ".datasource-list")).toContain("overflow-y: auto");
    expect(rule(consoleCss, ".columns-table")).toContain("overflow: auto");
    expect(rule(consoleCss, ".code-editor")).toContain("overflow: auto");
    expect(rule(knowledgeCss, ".kw-rule-list")).toContain("overflow-y: auto");
    expect(rule(knowledgeCss, ".kw-sql-list")).toContain("overflow-y: auto");
    expect(rule(viewCss, ".view-tab-panel")).toContain("overflow-y: auto");
    expect(rule(cubeCss, ".cube-workspace-content")).toContain("overflow-y: auto");
    expect(rule(accessCss, ".access-audit-scroll")).toContain("overflow: auto");
  });

  it("reserves space for the fixed command bar", () => {
    expect(rule(consoleCss, ".content")).toContain("padding: 31px 38px 85px");
    expect(rule(consoleCss, ".command-bar")).toContain("position: fixed");
  });

  it("lets the instructions editor shrink before its mobile breakpoint", () => {
    expect(rule(consoleCss, ".editor-layout")).toContain("grid-template-columns: minmax(0, 1.55fr) minmax(240px, 0.65fr)");
  });
});
