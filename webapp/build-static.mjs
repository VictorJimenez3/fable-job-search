import {mkdirSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";
import {build} from "vite";

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, "public");

rmSync(output, {recursive: true, force: true});
mkdirSync(output, {recursive: true});
await build({configFile: join(root, "vite.config.mts")});
const source = readFileSync(join(root, "index.html"), "utf8");
const buildSha = process.env.RADAR_BUILD_SHA?.trim() || "local";
const marker = `<meta name="job-radar-build" content="${buildSha}">`;
const html = source.replace('<meta charset="utf-8">', `<meta charset="utf-8">\n${marker}`);
if (html === source) throw new Error("could not insert build marker into webapp/index.html");
writeFileSync(join(output, "index.html"), html);
