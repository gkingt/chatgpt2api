const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const ts = require("typescript");

function config(revision = 0) {
  return {
    config_revision: revision, enabled: false, proxy: "", total: 10, threads: 3,
    mode: "total", target_quota: 100, target_available: 10, check_interval: 5,
    mail: { request_timeout: 30, wait_timeout: 120, wait_interval: 3,
      providers: [{ id: "mail", type: "tempy_email", enable: true, api_key: "saved" }] },
    stats: { success: 0, fail: 0, done: 0, running: 0, threads: 3 }, logs: [],
  };
}

function storeWith(api = {}) {
  const source = ts.transpileModule(readFileSync(join(__dirname, "../src/app/settings/store.ts"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  const messages = [];
  const toast = new Proxy({}, { get: (_, level) => (message) => messages.push({ level, message }) });
  const load = (name) => name === "@/lib/api" ? api : name === "sonner" ? { toast } : require(name);
  new Function("require", "module", "exports", source)(load, module, module.exports);
  const store = module.exports.useSettingsStore;
  store.getState().setRegisterConfig(config());
  return { store, messages };
}

test("SSE updates runtime without erasing unsaved provider edits", () => {
  const { store } = storeWith();
  store.getState().updateRegisterProvider(0, { api_key: "unsaved" });
  store.getState().setRegisterProxy("http://draft:8080");
  const snapshot = config();
  snapshot.stats.fail = 7;
  store.getState().setRegisterConfig(snapshot);
  assert.equal(store.getState().registerConfig.mail.providers[0].api_key, "unsaved");
  assert.equal(store.getState().registerConfig.proxy, "http://draft:8080");
  assert.equal(store.getState().registerConfig.stats.fail, 7);
  assert.equal(store.getState().isRegisterDirty, true);
});

test("stale snapshots do not roll back a newly saved configuration", () => {
  const { store } = storeWith();
  const saved = config(2);
  saved.mail.providers[0].api_key = "new";
  store.getState().setRegisterConfig(saved);
  store.getState().setRegisterConfig(config(1));
  assert.equal(store.getState().registerConfig.mail.providers[0].api_key, "new");
});

test("edits made during save survive the save response", async () => {
  let resolve;
  const { store } = storeWith({ updateRegisterConfig: () => new Promise((done) => { resolve = done; }) });
  store.getState().setRegisterTotal("20");
  const pending = store.getState().saveRegister();
  store.getState().setRegisterTotal("30");
  const saved = config(1);
  saved.total = 20;
  resolve({ register: saved });
  await pending;
  assert.equal(store.getState().registerConfig.total, 30);
  assert.equal(store.getState().registerConfig.config_revision, 1);
  assert.equal(store.getState().isRegisterDirty, true);
});

test("stop remains available during a pending save operation", async () => {
  let called = 0;
  const { store } = storeWith({ stopRegister: async () => { called++; return { register: config() }; } });
  store.setState({ registerConfig: { ...config(), enabled: true }, isSavingRegister: true });
  await store.getState().toggleRegister();
  assert.equal(called, 1);
  assert.equal(store.getState().registerConfig.enabled, false);
  assert.equal(store.getState().isStoppingRegister, false);
});

test("failed save leaves draft intact and releases loading state", async () => {
  const { store } = storeWith({ updateRegisterConfig: async () => { throw new Error("disk full"); } });
  store.getState().setRegisterTotal("25");
  await store.getState().saveRegister();
  assert.equal(store.getState().registerConfig.total, 25);
  assert.equal(store.getState().isRegisterDirty, true);
  assert.equal(store.getState().isSavingRegister, false);
});

test("successful save clears dirty state and sends current revision", async () => {
  let sent;
  const { store } = storeWith({ updateRegisterConfig: async (body) => { sent = body; return { register: { ...config(1), ...body, config_revision: 1 } }; } });
  store.getState().setRegisterTotal("25");
  await store.getState().saveRegister();
  assert.equal(sent.config_revision, 0);
  assert.equal(store.getState().isRegisterDirty, false);
  assert.equal(store.getState().registerConfig.config_revision, 1);
});

test("stats reset preserves unsaved form edits", async () => {
  const { store } = storeWith({ resetRegister: async () => ({ register: config() }) });
  store.getState().setRegisterTotal("25");
  await store.getState().resetRegister();
  assert.equal(store.getState().registerConfig.total, 25);
  assert.equal(store.getState().isRegisterDirty, true);
});

test("edits made while start is pending survive the start response", async () => {
  let resolveStart;
  const { store } = storeWith({
    updateRegisterConfig: async () => ({ register: config(1) }),
    startRegister: () => new Promise((done) => { resolveStart = done; }),
  });
  const pending = store.getState().toggleRegister();
  await Promise.resolve();
  assert.equal(typeof resolveStart, "function");
  store.getState().setRegisterTotal("35");
  resolveStart({ register: { ...config(1), enabled: true } });
  await pending;
  assert.equal(store.getState().registerConfig.total, 35);
  assert.equal(store.getState().registerConfig.enabled, true);
  assert.equal(store.getState().isRegisterDirty, true);
});
