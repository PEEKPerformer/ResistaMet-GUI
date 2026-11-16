import assert from "node:assert/strict";
import { test } from "node:test";

import {
  changeRefusal,
  dataDirectoryHint,
  isAbsolutePath,
  needsReload,
  resolveDataDirectory,
  shouldAskForDataFolder,
  type DataFolderState,
} from "./dataFolder.ts";

const DEFAULT = "C:\\Users\\lab\\Documents\\ResistaMet";

function state(over: Partial<DataFolderState> = {}): DataFolderState {
  return {
    managed: true,
    folder: DEFAULT,
    chosen: false,
    default_folder: DEFAULT,
    default_is_fallback: false,
    unavailable: null,
    legacy: null,
    ...over,
  };
}

test("an installed app with nothing chosen asks", () => {
  assert.equal(shouldAskForDataFolder(state(), false), true);
});

test("a chosen folder is not asked about again, the default included", () => {
  assert.equal(shouldAskForDataFolder(state({ chosen: true }), false), false);
  assert.equal(shouldAskForDataFolder(state({ chosen: true, folder: "D:\\data" }), false), false);
});

test("a stored folder that could not be used asks again", () => {
  assert.equal(shouldAskForDataFolder(state({ unavailable: "E:\\gone" }), false), true);
});

test("a dismissal holds for the launch, and a source checkout or a browser never asks", () => {
  assert.equal(shouldAskForDataFolder(state(), true), false);
  assert.equal(shouldAskForDataFolder(state({ unavailable: "E:\\gone" }), true), false);
  assert.equal(shouldAskForDataFolder(state({ managed: false }), false), false);
  assert.equal(shouldAskForDataFolder(null, false), false);
});

test("the folder changes only on an idle session that answered", () => {
  assert.equal(changeRefusal({ state: "idle" }), null);
  for (const s of ["identifying", "running", "paused", "awaiting_prompt", "stopping"] as const) {
    assert.equal(changeRefusal({ state: s }), "A run is in progress.");
  }
  assert.equal(changeRefusal(null), "The backend is not answering.");
});

test("only a change of the folder in use needs the page reloaded", () => {
  assert.equal(needsReload(state(), state({ chosen: true })), false);
  assert.equal(needsReload(state(), state({ chosen: true, folder: "D:\\data" })), true);
});

test("absolute paths are recognised on both platforms", () => {
  for (const path of ["/Users/lab/data", "C:\\data", "c:/data", "\\\\nas\\share\\data"]) {
    assert.equal(isAbsolutePath(path), true, path);
  }
  for (const path of ["measurement_data", "./data", "..\\data", "C:data", ""]) {
    assert.equal(isAbsolutePath(path), false, path);
  }
});

test("a relative data directory lands inside the data folder, an absolute one where it says", () => {
  assert.equal(resolveDataDirectory("measurement_data", DEFAULT), `${DEFAULT}\\measurement_data`);
  assert.equal(resolveDataDirectory("measurement_data", "/Users/lab/Documents/ResistaMet/"), "/Users/lab/Documents/ResistaMet/measurement_data");
  assert.equal(resolveDataDirectory("./runs", "/data"), "/data/runs");
  assert.equal(resolveDataDirectory("D:\\runs", DEFAULT), "D:\\runs");
  assert.equal(resolveDataDirectory("measurement_data", null), null);
  assert.equal(resolveDataDirectory("  ", DEFAULT), null);
});

test("the hint is the backend's answer for the stored value and a resolution for an edit", () => {
  const root = `${DEFAULT}\\measurement_data`;
  assert.equal(dataDirectoryHint("measurement_data", "measurement_data", root, null), `Resolves to ${root}`);
  assert.equal(dataDirectoryHint("runs", "measurement_data", root, DEFAULT), `Resolves to ${DEFAULT}\\runs`);
  assert.equal(dataDirectoryHint("runs", "measurement_data", root, null), null);
  // Before the backend has answered, the stored value resolves the same way.
  assert.equal(dataDirectoryHint("measurement_data", "measurement_data", null, DEFAULT), `Resolves to ${root}`);
});
