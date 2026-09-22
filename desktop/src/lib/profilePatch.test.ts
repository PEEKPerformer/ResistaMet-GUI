import { test } from "node:test";
import assert from "node:assert/strict";
import { profilePatch, withMachineLocal } from "./profilePatch.ts";

const LOADED = {
  measurement: { nplc: 1, sampling_rate: 10, gpib_address: "GPIB0::24::INSTR", visa_library: "", gpib_interface: "" },
  file: { data_directory: "data" },
  output: { format: "csv" },
};

test("only the keys that changed are sent, and only their sections", () => {
  const draft = structuredClone(LOADED);
  draft.measurement.nplc = 5;
  assert.deepEqual(profilePatch(LOADED, draft), { measurement: { nplc: 5 } });
  assert.deepEqual(profilePatch(LOADED, structuredClone(LOADED)), {});
});

test("the address identified since the profile was loaded is not sent back", () => {
  // The draft still holds the address from before Identify stored a new one.
  const draft = structuredClone(LOADED);
  draft.measurement.sampling_rate = 2;
  const stored = structuredClone(LOADED);
  stored.measurement.gpib_address = "GPIB0::5::INSTR";
  assert.deepEqual(profilePatch(stored, draft), { measurement: { sampling_rate: 2 } });
});

test("a machine-local key is never sent, even when it is all that differs", () => {
  const draft = structuredClone(LOADED);
  draft.measurement.gpib_address = "GPIB0::9::INSTR";
  draft.measurement.visa_library = "@py";
  draft.measurement.gpib_interface = "PRLGX-ASRL::5::INTFC";
  assert.deepEqual(profilePatch(LOADED, draft), {});
});

test("a key the loaded profile did not have is sent", () => {
  const draft = { ...structuredClone(LOADED), display: { enable_plot: false } };
  assert.deepEqual(profilePatch(LOADED, draft), { display: { enable_plot: false } });
});

test("after Identify the draft takes the stored address and keeps its edits", () => {
  const draft = structuredClone(LOADED);
  draft.measurement.nplc = 5;
  const stored = structuredClone(LOADED);
  stored.measurement.gpib_address = "GPIB0::5::INSTR";
  stored.measurement.visa_library = "@py";
  const next = withMachineLocal(draft, stored);
  assert.equal(next.measurement?.gpib_address, "GPIB0::5::INSTR");
  assert.equal(next.measurement?.visa_library, "@py");
  assert.equal(next.measurement?.nplc, 5);
  assert.deepEqual(profilePatch(stored, next), { measurement: { nplc: 5 } });
});

test("a reply's non-section keys are skipped rather than walked", () => {
  // GET /profiles once carried the config-level `users` list and a null
  // `last_user` beside the sections; Object.entries(null) threw and took the
  // Settings dialog down with it.
  const loaded = { ...structuredClone(LOADED), users: [] as unknown as Record<string, unknown>, last_user: null as unknown as Record<string, unknown> };
  const draft = structuredClone(loaded);
  draft.measurement.nplc = 5;
  assert.deepEqual(profilePatch(loaded, draft), { measurement: { nplc: 5 } });
});
