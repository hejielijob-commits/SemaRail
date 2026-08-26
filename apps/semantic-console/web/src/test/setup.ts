import "@testing-library/jest-dom/vitest";

// jsdom does not implement DOMMatrixReadOnly. React Flow only reads m22 from
// it while recalculating dynamic node handles, so this faithful identity
// matrix keeps those lifecycle paths testable without suppressing updates.
if (typeof window !== "undefined" && typeof window.DOMMatrixReadOnly !== "function") {
  class DOMMatrixReadOnlyStub {
    readonly m22 = 1;
  }
  Object.defineProperty(window, "DOMMatrixReadOnly", { configurable: true, value: DOMMatrixReadOnlyStub });
}
