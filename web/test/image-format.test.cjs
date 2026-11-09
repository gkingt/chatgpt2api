const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const ts = require("typescript");

function loadModule(path, load = require) {
  const source = ts.transpileModule(readFileSync(join(__dirname, path), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", source)(load, module, module.exports);
  return module.exports;
}

const formats = loadModule("../src/lib/image-format.ts");

test("downloads derive native extensions and MIME types", () => {
  for (const [output_format, mime, ext] of [["gif", "image/gif", "gif"], ["svg", "image/svg+xml", "svg"], ["jpeg", "image/jpeg", "jpg"], ["webp", "image/webp", "webp"]]) {
    assert.equal(formats.imageExtension({ output_format }), ext);
    assert.equal(formats.imageMimeType({ output_format }), mime);
    assert.equal(formats.imageExtension({ mime_type: mime }), ext);
  }
  assert.equal(formats.imageExtension({ url: "https://app.test/a.GIF?download=1" }), "gif");
  assert.equal(formats.imageMimeType({}), "image/png");
});

test("switching to ChatGPT web clears Codex-only options without losing GIF timing", () => {
  const options = { output_format: "gif", frame_count: 8, frame_duration_ms: 300, complexity: "low" };
  assert.deepEqual(formats.imageOptionsForModel("gpt-image-2.6-flare", options), { output_format: "gif", frame_count: 8, frame_duration_ms: 300 });
  assert.deepEqual(formats.imageOptionsForModel("gpt-image-2.6-sunburst", { output_format: "svg", complexity: "high" }), { output_format: "png" });
  assert.deepEqual(formats.imageOptionsForModel("gpt-image-2", options), { output_format: "png" });
  assert.deepEqual(formats.imageOptionsForModel("codex-gpt-image-2.6-flare", options), options);
});

test("generation and multipart edit tasks forward animation options", async () => {
  const calls = [];
  const api = loadModule("../src/lib/api.ts", (name) => name === "@/lib/request" ? { httpRequest: async (...args) => { calls.push(args); return {}; } } : require(name));
  const options = { output_format: "gif", frame_count: 8, frame_duration_ms: 300, complexity: "low" };
  await api.createImageGenerationTask("gif", "animate", "codex-gpt-image-2.6-flare", "auto", "high", options);
  assert.equal(calls[0][1].body.output_format, "gif");
  assert.equal(calls[0][1].body.frame_count, 8);
  await api.createImageEditTask("edit", new File(["png"], "a.png", { type: "image/png" }), "animate", "codex-gpt-image-2.6-flare", "auto", "high", options);
  assert.equal(calls[1][1].body.get("output_format"), "gif");
  assert.equal(calls[1][1].body.get("frame_duration_ms"), "300");
  assert.equal(calls[1][1].body.getAll("image").length, 1);
});

test("conversation history retains format options and native image metadata", async () => {
  let saved = [{ id: "conversation", updatedAt: "2099-01-01", turns: [{ id: "turn", model: "codex-gpt-image-2.6-flare", outputOptions: { output_format: "gif", frame_count: 8 }, images: [{ id: "image", url: "https://app/a.gif", output_format: "gif", mime_type: "image/gif" }] }] }];
  const storage = { getItem: async () => saved, setItem: async (_, value) => { saved = value; } };
  const history = loadModule("../src/store/image-conversations.ts", (name) => name === "localforage" ? { default: { createInstance: () => storage } } : require(name));
  const conversations = await history.listImageConversations();
  assert.equal(conversations[0].turns[0].outputOptions.frame_count, 8);
  assert.equal(conversations[0].turns[0].images[0].mime_type, "image/gif");
});
