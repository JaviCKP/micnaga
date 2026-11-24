"use strict";

const { spawn, spawnSync } = require("node:child_process");
const { existsSync } = require("node:fs");
const { homedir } = require("node:os");
const path = require("node:path");

function main() {
  const localAppData = process.env.LOCALAPPDATA || path.join(homedir(), "AppData", "Local");
  const cli = process.env.MICNAGA_CLI?.trim() ||
    path.join(localAppData, "Programs", "Micnaga", "micnaga-cli.exe");
  if (!/^[a-z]:[\\/]/i.test(cli) || !/^micnaga(?:-cli)?\.exe$/i.test(path.basename(cli))) {
    throw new Error("Selecciona la ruta absoluta de micnaga-cli.exe.");
  }
  if (!existsSync(cli)) {
    throw new Error("Instala Micnaga o indica la ruta de su ejecutable en los ajustes de la extensión.");
  }

  const env = Object.fromEntries(
    Object.entries(process.env).filter(([name]) => !/^MICNAGA_ODOO_/i.test(name)),
  );
  env.PYTHONIOENCODING = "utf-8";
  env.PYTHONUNBUFFERED = "1";
  const options = { env, windowsHide: true };
  // Las versiones antiguas ignoran el argumento y arrancan el servidor de la Secretaria.
  const help = spawnSync(cli, ["tools", "--help"], {
    ...options, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 8000,
  });
  if (help.status !== 0 || !/(^|\s)--library\b/.test(help.stdout || "")) {
    throw new Error("Actualiza Micnaga a una versión compatible con el conector de reuniones.");
  }

  const child = spawn(cli, ["tools", "--library"], { ...options, stdio: "inherit" });
  child.once("error", (error) => {
    console.error(`Micnaga: ${error.message}`);
    process.exitCode = 1;
  });
  child.once("exit", (code) => { process.exitCode = code ?? 1; });
}

try {
  main();
} catch (error) {
  console.error(`Micnaga: ${error.message}`);
  process.exitCode = 1;
}
