import {copyFileSync, mkdirSync, rmSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";
import {build} from "vite";

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, "public");

rmSync(output, {recursive: true, force: true});
mkdirSync(output, {recursive: true});
await build({configFile: join(root, "vite.config.mts")});
copyFileSync(join(root, "index.html"), join(output, "index.html"));
