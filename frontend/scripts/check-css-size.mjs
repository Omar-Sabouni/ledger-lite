import { readdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const CSS_SIZE_CEILING = 26_450;

export async function measureCssAssets(assetsDirectory) {
  const entries = await readdir(assetsDirectory, { withFileTypes: true });
  const names = entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".css"))
    .map((entry) => entry.name)
    .sort();

  if (names.length === 0) {
    throw new Error(`CSS size check found no .css files in ${assetsDirectory}`);
  }

  const files = await Promise.all(
    names.map(async (name) => ({
      name,
      bytes: (await stat(join(assetsDirectory, name))).size
    }))
  );

  return {
    files,
    totalBytes: files.reduce((total, file) => total + file.bytes, 0)
  };
}

export function enforceCssBudget(measurement, ceiling = CSS_SIZE_CEILING) {
  if (measurement.totalBytes > ceiling) {
    const overage = measurement.totalBytes - ceiling;
    const assets = measurement.files
      .map((file) => `${file.name}: ${file.bytes.toLocaleString("en-US")} B`)
      .join(", ");

    throw new Error(
      `CSS size budget exceeded: ${measurement.totalBytes.toLocaleString("en-US")} B emitted; ` +
        `ceiling ${ceiling.toLocaleString("en-US")} B; ${overage.toLocaleString("en-US")} B over. ` +
        `Assets: ${assets}`
    );
  }

  return ceiling - measurement.totalBytes;
}

async function main() {
  const assetsDirectory = fileURLToPath(new URL("../dist/assets/", import.meta.url));
  const measurement = await measureCssAssets(assetsDirectory);
  const remaining = enforceCssBudget(measurement);

  console.log(
    `CSS size budget: ${measurement.totalBytes.toLocaleString("en-US")} / ` +
      `${CSS_SIZE_CEILING.toLocaleString("en-US")} B ` +
      `(${remaining.toLocaleString("en-US")} B remaining across ${measurement.files.length} asset${measurement.files.length === 1 ? "" : "s"}).`
  );
}

const invokedPath = process.argv[1];
if (invokedPath && import.meta.url === pathToFileURL(invokedPath).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
