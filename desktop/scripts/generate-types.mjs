#!/usr/bin/env node
// Generate TypeScript types from the backend's JSON Schema contracts.
//
// The pydantic models in resistamet_gui/schema and resistamet_gui/session are
// the single source of truth; tools/export_contracts.py renders them to
// ../contracts/*.schema.json, and this turns those into src/generated/*.ts.
// Nothing in the UI hand-copies a field name, so a backend change shows up as
// a type error here rather than as a runtime surprise in the lab.
//
//   node scripts/generate-types.mjs          # write
//   node scripts/generate-types.mjs --check  # fail if committed output is stale
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "..", "..", "contracts");
const outDir = resolve(here, "..", "src", "generated");
const check = process.argv.includes("--check");

const BANNER =
  "/* eslint-disable */\n" +
  "// GENERATED from contracts/*.schema.json by desktop/scripts/generate-types.mjs.\n" +
  "// Do not edit. Regenerate with `npm run gen:types`.\n\n";

const compileOptions = {
  bannerComment: "",
  additionalProperties: false,
  // pydantic emits "title" for every field; as doc comments they only add noise.
  ignoreMinAndMaxItems: true,
  strictIndexSignatures: true,
  style: { singleQuote: false, semi: true, trailingComma: "all" },
};

function prepare(schema, { exact }) {
  // pydantic puts a `title` on every model and every field. On a model it
  // overrides the name we pass to compile(); on a field it makes the generator
  // emit a separate type alias per property, which buries the interfaces in
  // noise. Strip them all and name things ourselves ($defs keep their keys).
  //
  // `exact` drops `additionalProperties: true`: the settings models accept
  // unknown keys so an old profile still opens, but the UI only ever sends
  // known ones, and an index signature on every model would defeat the point
  // of having types.
  const clone = JSON.parse(JSON.stringify(schema));
  const walk = (node) => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (node && typeof node === "object") {
      delete node.title;
      if (exact && node.additionalProperties === true) delete node.additionalProperties;
      for (const value of Object.values(node)) walk(value);
    }
  };
  walk(clone);
  return clone;
}

async function compileDefinition(schema, name, { exact = false } = {}) {
  // Each pydantic model schema is self-contained (nested models live in
  // $defs), so it compiles on its own. A nested model that is also a top-level
  // definition would be emitted twice; dedupeInterfaces keeps the first.
  return compile(prepare(schema, { exact }), name, compileOptions);
}

function dedupeInterfaces(source) {
  // json-schema-to-typescript emits a nested model's interface wherever it is
  // referenced. Keep the first definition of each exported name.
  const seen = new Set();
  const blocks = source.split(/\n(?=export )/);
  const kept = [];
  for (const block of blocks) {
    const match = block.match(/^export (?:interface|type) (\w+)/);
    if (match) {
      if (seen.has(match[1])) continue;
      seen.add(match[1]);
    }
    kept.push(block);
  }
  return kept.join("\n");
}

function fieldMeta(contract) {
  // Per-field bounds, defaults and enums, straight from the schema, so a form
  // can clamp and validate exactly where the backend will. Labels and units
  // are a UI concern and live in src/lib/fields.ts.
  const meta = {};
  for (const [model, schema] of Object.entries(contract.definitions)) {
    const fields = {};
    for (const [key, prop] of Object.entries(schema.properties ?? {})) {
      const entry = {};
      const resolved = prop.anyOf ? prop.anyOf.find((p) => p.type !== "null") ?? prop : prop;
      if (resolved.type) entry.type = resolved.type;
      if (prop.anyOf?.some((p) => p.type === "null")) entry.nullable = true;
      if (resolved.enum) entry.enum = resolved.enum;
      if (resolved.minimum !== undefined) entry.min = resolved.minimum;
      if (resolved.exclusiveMinimum !== undefined) entry.exclusiveMin = resolved.exclusiveMinimum;
      if (resolved.maximum !== undefined) entry.max = resolved.maximum;
      if (resolved.exclusiveMaximum !== undefined) entry.exclusiveMax = resolved.exclusiveMaximum;
      if ("default" in prop) entry.default = prop.default;
      if (schema.required?.includes(key)) entry.required = true;
      fields[key] = entry;
    }
    meta[model] = fields;
  }
  return meta;
}

async function settingsTypes() {
  const contract = JSON.parse(readFileSync(join(contractsDir, "settings.schema.json"), "utf8"));
  const parts = [];
  for (const [name, schema] of Object.entries(contract.definitions)) {
    parts.push(await compileDefinition(schema, name, { exact: true }));
  }
  const modes = Object.entries(contract.modes);
  parts.push(
    "/** Measurement modes and the settings model each one uses. */\n" +
      "export const MODES = [" +
      modes.map(([mode]) => JSON.stringify(mode)).join(", ") +
      "] as const;\n" +
      "export type Mode = (typeof MODES)[number];\n\n" +
      "export interface ModeSettingsMap {\n" +
      modes.map(([mode, model]) => `  ${mode}: ${model};`).join("\n") +
      "\n}\n\n" +
      "/** The settings model each mode's tab writes, by mode. */\n" +
      "export const MODE_MODEL = {\n" +
      modes.map(([mode, model]) => `  ${mode}: ${JSON.stringify(model)},`).join("\n") +
      "\n} as const;\n",
  );
  parts.push(
    "export interface FieldMeta {\n" +
      "  type?: string;\n  nullable?: boolean;\n  enum?: readonly (string | number)[];\n" +
      "  min?: number;\n  exclusiveMin?: number;\n  max?: number;\n  exclusiveMax?: number;\n" +
      "  default?: unknown;\n  required?: boolean;\n}\n\n" +
      "/** Bounds, defaults and enums per model field, from the schema. */\n" +
      "export const FIELD_META: Record<string, Record<string, FieldMeta>> = " +
      JSON.stringify(fieldMeta(contract), null, 2) +
      ";\n",
  );
  return BANNER + dedupeInterfaces(parts.join("\n"));
}

async function eventTypes() {
  const contract = JSON.parse(readFileSync(join(contractsDir, "events.schema.json"), "utf8"));
  const parts = [];
  // The envelope's `payload` is a free-form object in the schema; the typed
  // Event<T> below narrows it per event type, so here it stays `unknown`.
  const envelope = JSON.parse(JSON.stringify(contract.envelope));
  envelope.properties.payload = { type: "object", additionalProperties: true };
  parts.push(await compileDefinition(envelope, "EventEnvelope"));
  const payloadNames = [];
  for (const [type, schema] of Object.entries(contract.payloads)) {
    // Several event types share one payload model (paused/resumed/stopping);
    // the model's own name keeps them as one interface.
    const name = schema.title || pascal(type) + "Payload";
    payloadNames.push([type, name]);
    parts.push(await compileDefinition(schema, name));
  }
  parts.push(
    `export const EVENT_SCHEMA_VERSION = ${JSON.stringify(contract.version)};\n\n` +
      "/** Event type -> payload. Events not listed here carry an untyped payload. */\n" +
      "export interface EventPayloadMap {\n" +
      payloadNames.map(([type, name]) => `  ${type}: ${name};`).join("\n") +
      "\n}\n\n" +
      "export type EventType = keyof EventPayloadMap;\n\n" +
      "/** A typed event: the envelope with its payload narrowed by `type`. */\n" +
      "export type Event<T extends EventType = EventType> = Omit<EventEnvelope, \"type\" | \"payload\"> & {\n" +
      "  type: T;\n" +
      "  payload: EventPayloadMap[T];\n" +
      "};\n\n" +
      "export type AnyEvent = { [T in EventType]: Event<T> }[EventType];\n",
  );
  return BANNER + dedupeInterfaces(parts.join("\n"));
}

async function mapTypes() {
  // What GET /maps/{map_id} returns. Nested models (the spots, their
  // statistics) come out of the one definition's $defs.
  const contract = JSON.parse(readFileSync(join(contractsDir, "maps.schema.json"), "utf8"));
  const parts = [];
  for (const [name, schema] of Object.entries(contract.definitions)) {
    parts.push(await compileDefinition(schema, name));
  }
  return BANNER + dedupeInterfaces(parts.join("\n"));
}

function pascal(snake) {
  return snake.split("_").map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join("");
}

async function main() {
  const outputs = {
    "settings.ts": await settingsTypes(),
    "events.ts": await eventTypes(),
    "maps.ts": await mapTypes(),
  };
  mkdirSync(outDir, { recursive: true });
  let stale = false;
  for (const [file, content] of Object.entries(outputs)) {
    const path = join(outDir, file);
    if (check) {
      // A Windows checkout with autocrlf hands us CRLF; the generator writes LF.
      // Line endings are not a contract change.
      const current = existsSync(path) ? readFileSync(path, "utf8").replace(/\r\n/g, "\n") : "";
      if (current !== content) {
        console.error(`stale: src/generated/${file} — run npm run gen:types`);
        stale = true;
      }
    } else {
      writeFileSync(path, content);
      console.log(`wrote src/generated/${file}`);
    }
  }
  if (stale) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
