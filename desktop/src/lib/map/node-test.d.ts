// Just enough of Node's test runner for the type checker. The package has no
// @types/node and the tests use nothing beyond this.

declare module "node:test" {
  export function test(name: string, fn: () => void | Promise<void>): void;
}

declare module "node:assert/strict" {
  interface Assert {
    (value: unknown, message?: string): asserts value;
    ok(value: unknown, message?: string): asserts value;
    equal(actual: unknown, expected: unknown, message?: string): void;
    notEqual(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    match(actual: string, pattern: RegExp, message?: string): void;
    doesNotMatch(actual: string, pattern: RegExp, message?: string): void;
  }
  const assert: Assert;
  export default assert;
}
