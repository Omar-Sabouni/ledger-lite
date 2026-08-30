import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { enforceCssBudget, measureCssAssets } from "./check-css-size.mjs";

test("measures all emitted CSS and ignores other assets", async (context) => {
  const directory = await mkdtemp(join(tmpdir(), "ledgerlite-css-budget-"));
  context.after(() => rm(directory, { recursive: true, force: true }));
  await Promise.all([
    writeFile(join(directory, "base.css"), "1234567890"),
    writeFile(join(directory, "route.css"), "12345"),
    writeFile(join(directory, "app.js"), "ignored")
  ]);

  const measurement = await measureCssAssets(directory);

  assert.equal(measurement.totalBytes, 15);
  assert.deepEqual(measurement.files, [
    { name: "base.css", bytes: 10 },
    { name: "route.css", bytes: 5 }
  ]);
  assert.equal(enforceCssBudget(measurement, 20), 5);
});

test("reports the measured overage and contributing assets", () => {
  const measurement = {
    totalBytes: 15,
    files: [{ name: "app.css", bytes: 15 }]
  };

  assert.throws(
    () => enforceCssBudget(measurement, 10),
    /CSS size budget exceeded: 15 B emitted; ceiling 10 B; 5 B over\. Assets: app\.css: 15 B/
  );
});
