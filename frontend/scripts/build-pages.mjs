import { readFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { config } from "../config.js";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = resolve(projectRoot, "dist", "pages");
if (!output.startsWith(projectRoot + sep)) throw new Error("Путь сборки выходит за пределы проекта.");
if (config.mode !== "demo") throw new Error("Публикация GitHub Pages настроена только для явно помеченного деморежима.");

rmSync(output, { recursive: true, force: true });
mkdirSync(join(output, "src"), { recursive: true });
for (const name of ["index.html", "styles.css", "config.js"]) writeFileSync(join(output, name), readFileSync(join(projectRoot, name)));
for (const name of ["app.js", "api.js", "demo-api.js", "webauthn.js", "auth.js", "device.js", "view.js", "zones.js", "landing.js", "camera.js"]) writeFileSync(join(output, "src", name), readFileSync(join(projectRoot, "src", name)));
mkdirSync(join(output, "src", "assets"), { recursive: true });
for (const name of ["betonomeshalka.jpg", "kotlovan.jpg", "kurilka.jpg"]) writeFileSync(join(output, "src", "assets", name), readFileSync(join(projectRoot, "src", "assets", name)));
writeFileSync(join(output, ".nojekyll"), "");
console.log(`Готов статический сайт: ${output}`);
